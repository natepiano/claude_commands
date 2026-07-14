---
description: Rank the open issue backlog in the Hanadocs Obsidian vault and update its global and project Base views through a calibrated multi-agent review.
---

**DRAFT — do not execute this command while this marker remains.**

Prioritize issues in the Hanadocs Obsidian vault using a repeatable rubric and a team of read-only review agents. The primary agent is the only writer.

## Fixed scope

- Vault: `/Users/natemccoy/rust/hanadocs`
- Issues: `/Users/natemccoy/rust/hanadocs/issues/*.md`
- Bases: `/Users/natemccoy/rust/hanadocs/*.base`
- Strategic goals: `/Users/natemccoy/rust/hanadocs/prioritization goals.md`
- Never derive the vault from the current working directory.
- Never inspect or modify issues outside this vault.

## Settled contract

### Canonical ordering

- Store one canonical global ordering in the `backlog_rank` frontmatter property.
- Write `backlog_rank` as an unquoted integer; lower numbers mean higher priority.
- Use the same global rank in every project Base. Project views are filtered subsets, so gaps in their displayed rank numbers are expected.
- Do not create separate per-project rank properties.

### Eligibility

- Rank every issue whose `status` is `open`.
- Include open issues at every stage, including `backlog`, `active`, and `waiting`.
- Exclude closed issues from the live backlog ordering.

### Governing criterion and rubric metadata

- Make alignment with current strategic goals the governing prioritization criterion.
- Read the canonical ordered goals from `/Users/natemccoy/rust/hanadocs/prioritization goals.md`; do not infer or replace them from the issue corpus.
- Give earlier goals more influence through soft weighting, not absolute precedence. A strongly justified issue for a later goal may outrank a weakly justified issue for an earlier goal.
- Never group the final backlog into rigid goal lanes where every issue for one goal outranks every issue for the next.
- Store the single goal an issue influences most in the scalar `strategic_goal` frontmatter property. Its value must exactly match one readable, numerically prefixed domain value defined in the goals note, for example `"1 - Ship Hana"`.
- Assign exactly one `strategic_goal` to every eligible issue; do not store multiple goals or a goal list.
- Use `strategic_alignment` to record how strongly the issue advances its selected goal.
- Record each rubric component as its own frontmatter property so Obsidian Bases can filter and sort it independently.
- Store `strategic_alignment`, `impact`, `urgency`, `leverage`, `confidence`, and `effort` as separate quoted strings in the exact form `"N - Mnemonic"`, where `N` is an integer from `0` through `5` and the mnemonic matches the domain definition below.
- Parse and validate the leading numeric prefix for scoring. Treat a missing rubric property as unassessed; a `0` prefix is an explicit assessed value.
- Store the computed `backlog_score` and `backlog_rank` as unquoted numbers.
- Use the existing `category` property for issue-type questions such as the highest-ranked bug, feature, business, or research issues; do not duplicate issue type in the rubric fields.
- Do not encode the rubric as a list or as `key=value` strings inside one property.
- Define every scale and anchor explicitly in this command so agents apply the metadata consistently.

### Effort sizing

- Store relative effort as a quoted prefixed domain string in `effort`.
- Interpret the values as `"0 - Negligible"`, `"1 - XS"`, `"2 - S"`, `"3 - M"`, `"4 - L"`, and `"5 - XL"`.
- Use `effort` only as a relative effort and complexity estimate.
- Never estimate or display real durations such as hours, days, or weeks.
- Never translate an effort value or t-shirt label into a real duration.

### Score model

- Compute the candidate backlog order with a weighted additive score.
- Combine weighted strategic alignment, a soft goal-order bonus, impact, urgency, leverage, a confidence adjustment, and a modest subtractive effort penalty.
- Never divide by `effort`; coarse effort estimates must not make tiny tasks dominate or make an `XL` strategic initiative disappear.
- Do not allow one zero-valued factor to collapse the entire score.
- Keep the exact weights explicit and centralized in this command (weights TBD).
- Parse the readable rubric strings, then write the resulting numeric `backlog_score` and `backlog_rank` together.
- Do not require calculated Base fields for the canonical score or rank. Base formulas may be added later for exploratory views, but they are not a source of truth.

### Rubric definitions

#### `strategic_alignment`

- `"0 - None"` — no meaningful relationship to the selected goal
- `"1 - Tangential"` — tangentially related
- `"2 - Indirect"` — indirectly supports the goal
- `"3 - Direct"` — directly advances the goal
- `"4 - Central"` — directly advances a central part of the goal
- `"5 - Core"` — completing the issue is itself a core goal outcome

Assign every eligible issue its closest `strategic_goal`; use `strategic_alignment: "0 - None"` when no current goal has a meaningful relationship.

#### `impact`

- `"0 - None"` — no meaningful benefit
- `"1 - Local"` — small or highly localized improvement
- `"2 - Narrow"` — modest benefit to a narrow workflow or audience
- `"3 - Meaningful"` — meaningful benefit to an important workflow or audience
- `"4 - Major"` — major benefit across a core workflow or multiple audiences
- `"5 - Transformative"` — transformative outcome for the product, organization, or ecosystem

Measure the magnitude of the benefit if completed. Do not include urgency, leverage, confidence, or effort in `impact`.

#### `urgency`

- `"0 - None"` — delaying has no material cost
- `"1 - Stable"` — value and opportunity remain largely unchanged
- `"2 - Rising"` — cost accumulates or opportunity modestly declines
- `"3 - Pressing"` — delay materially worsens the outcome or existing problem
- `"4 - Time-critical"` — an active commitment, dependency, or opportunity window is at risk
- `"5 - Immediate"` — serious ongoing harm or a hard external cutoff demands action

Measure cost of delay without estimating a duration. Require cited evidence for `urgency` values `4` and `5`; never invent a deadline, commitment, or closing opportunity window.

#### `leverage`

- `"0 - Isolated"` — no meaningful downstream enablement
- `"1 - Local"` — helps one nearby task or consumer
- `"2 - Reusable"` — helps several related tasks or consumers
- `"3 - Multiplier"` — enables multiple important issues or repeated use
- `"4 - Broad"` — unlocks major work across a project or several projects
- `"5 - Bottleneck"` — removes a foundational constraint blocking many valuable outcomes

Measure downstream enablement, not the issue's direct benefit. Require identifiable downstream work for high values; never assume that something described as foundational will be reused. The prioritization workflow owns and maintains this value rather than requiring the user to populate it manually.

#### `confidence`

- `"0 - Unknown"` — insufficient information to assess
- `"1 - Speculative"` — assumptions dominate and evidence is minimal
- `"2 - Tentative"` — partial evidence with important gaps
- `"3 - Supported"` — credible evidence with manageable uncertainty
- `"4 - Strong"` — direct evidence supports most material judgments
- `"5 - Verified"` — current, direct evidence supports the scope and expected outcome

Measure evidentiary support for the proposed rubric values, not confidence that the issue will succeed. Use low values to expose uncertainty rather than hiding agent guesses.

#### `effort`

- `"0 - Negligible"` — effectively no work or only an administrative action
- `"1 - XS"` — one atomic, tightly scoped action
- `"2 - S"` — contained work with few touchpoints and a known approach
- `"3 - M"` — several coordinated steps or touchpoints
- `"4 - L"` — broad work requiring substantial coordination or integration
- `"5 - XL"` — a multi-phase initiative, epic, or scope too broad for one task

Measure relative breadth and coordination, never elapsed time. Flag `effort: "5 - XL"` issues as likely decomposition candidates without automatically excluding or demoting them.

### Human approval

- Do not write proposed rubric values or ranks immediately after agent analysis.
- Present proposed values in manageable groups for user review.
- Show each issue as a row and each proposed frontmatter property as a column. At minimum, show `strategic_goal` and `strategic_alignment`.
- Render table cells with the stored short domain values such as `2 - Indirect` and `4 - Major`; do not repeat full definitions in every cell.
- Show the full factor legends once with each review group so the mnemonic labels remain unambiguous.
- Let the user approve a group or suggest changes before applying its values.
- Do not begin this review until the rubric and execution process are fully defined.
