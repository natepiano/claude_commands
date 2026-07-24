---
description: Focused comparative re-rating of one band of the Hanadocs backlog; the background watcher recomputes scores and ranks.
---

Redistribute one band of open issues in the Hanadocs Obsidian vault by re-rating them comparatively against each other, so their scores spread across the full rubric range instead of compressing into ties. Ratings enter the system through `/issue` at creation; `/prioritize` corrects compression later, in the band where rank actually drives decisions.

This command replaced the retired full-backlog multi-agent audit (2026-07-21). There are no reviewer or calibrator agent waves, no session manifests, and no hash-validated apply scripts: the primary agent reads the band, proposes ratings, the user corrects, and the frontmatter is edited directly. The background watcher owns all score and rank mechanics.

## Fixed scope

- Vault: `/Users/natemccoy/rust/hanadocs`
- Issues: `/Users/natemccoy/rust/hanadocs/issues/*.md`
- Strategic goals: `/Users/natemccoy/rust/hanadocs/prioritization goals.md`
- Rankings export: `/Users/natemccoy/rust/hanadocs/backlog-rankings.jsonl` (derived, watcher-written)
- Never derive the vault from the current working directory. Never inspect or modify issues outside this vault.

## Arguments — band selection

- `/prioritize` — band is `backlog_score >= 20` (the decision zone).
- `/prioritize N` — band is `backlog_score >= N`.
- `/prioritize N-M` — band is `N <= backlog_score <= M` (midrange sweeps).
- Reject anything else with `Usage: /prioritize [N | N-M]`.

Bands are rated internally against each other; different bands are only comparable through their pre-existing scores. Prefer one band per session.

## Workflow

1. **Preflight.** Run `/usr/bin/python3 /Users/natemccoy/.claude/scripts/prioritize/renumber.py` (dry-run). Require canonical state and a healthy watcher last-status in `/tmp/hanadocs-prioritize/`. List any issues reported as needing prioritization; they lack rubric values and can only be rated by the user (offer to survey them as in `/issue`), never invented.
2. **Enumerate the band** from `backlog-rankings.jsonl` plus each member's current `backlog_goal`, `backlog_alignment`, `backlog_impact`, `backlog_urgency`, `backlog_effort` frontmatter.
3. **Read every band issue in full.** No sampling. Note duplicate or overlapping scope between issues while reading.
4. **Propose comparative ratings.** Re-rate `backlog_alignment` and `backlog_impact` relative to the band, using the full one-to-five-star range — the strongest few issues in the band take five stars. Adjust `backlog_urgency` or `backlog_effort` only on explicit evidence in the note; their anchors stay absolute. Change `backlog_goal` only when the note clearly serves a different goal.
5. **Present one proposal table** in three groups — raised, lowered, unchanged — with current → proposed stars, the resulting score, and a one-phrase reason per changed row. Flag duplicate-scope issues as merge/close candidates. Wait for corrections; fold them in. Do not write before the user approves.
6. **Write the approved stars directly** to the issue frontmatter: only the five judgment fields, as double-quoted YAML star scalars (e.g. `backlog_alignment: "⭐⭐⭐⭐"`). Never write `backlog_score` or `backlog_rank` — the watcher owns them and recomputes within about a second.
7. **Verify and report.** Confirm the watcher status is `ok`/`updated`, re-read `backlog-rankings.jsonl`, check the expected scores landed, and show the band's before/after distribution (distinct scores, largest tie bucket, new head order). `renumber.py --check` must report no mechanical changes. Do not commit.

## Rating philosophy

- **Alignment and impact are comparative within the band.** Compression happens when every strong issue is rated against absolute anchors and lands at three or four stars; within a band, the top issues must occupy the top of the scale.
- **Urgency is absolute and rare by design.** It measures evidenced cost of delay; one star is the correct value for most of the backlog. Require cited evidence for four or five stars; never invent a deadline, commitment, or closing opportunity window.
- **Effort is absolute** relative sizing (XS–XL), never a duration.
- Score ties inside a band are acceptable when the issues are genuinely interchangeable; the goal is a credible ordering where decisions happen, not 371 unique scores.

## Goals maintenance

`prioritization goals.md` remains the canonical ordered goal list. Propose additions, removals, renames, or reordering conversationally with evidence from the backlog; apply them only after explicit user approval by editing the note directly, preserving the contiguous `N - Name` format and the definitions section. The watcher re-ranks automatically after a goals edit. Obsidian links compare by displayed text: `1 - Ship [[hana|Hana]]` matches the issue value `1 - Ship Hana`.

## Settled contract

### Canonical ordering

- One canonical global ordering lives in the `backlog_rank` frontmatter property: unquoted integer, lower is higher priority, dense `1..N` over eligible issues.
- Every project Base uses the same global rank; project views are filtered subsets, so displayed gaps are expected. No per-project rank properties.

### Workflow split

- `/issue` collects the five judgment fields from the user at creation.
- `/prioritize` redistributes a band comparatively, with user approval, and applies explicit user corrections at any time.
- The automatic watcher owns mechanics after any relevant edit from any source (this command, Obsidian, other processes): parse inputs, recompute `backlog_score`, densely renumber `backlog_rank`. The watcher never invents or revises judgment fields.
- No workflow writes a rank directly, and score ties are never escalated to the user for adjudication.

### Deterministic renumbering

- `/Users/natemccoy/.claude/scripts/prioritize/renumber.py` is the mechanical scoring and dense-renumbering tool; vault paths are hard-coded in it.
- Sort by `backlog_score` descending; equal scores preserve valid existing relative rank, with file path as the deterministic fallback for first runs and newly tied issues.
- Dry-run is the default; `--apply` is required to write. It preserves unrelated frontmatter, note content, permissions, APFS creation time, and the modification calendar date (a ~1 ms mtime nudge invalidates Obsidian's metadata cache without changing the date `obsidian_knife` sees).
- An open issue with missing or invalid rubric inputs loses any stale score/rank, is reported as needing prioritization, and never blocks ranking of valid issues.
- It refuses to apply when ranks would not be unique and contiguous, a file changed after discovery, or a write cannot complete safely.
- `strip_generated.py` is the vault's git clean filter: committed issue blobs carry no `backlog_score`/`backlog_rank`, so global re-ranks cause no per-file churn in git history. The working tree keeps the fields for Obsidian Base views.

### Automatic ranking watcher

- Source, setup/status helper, and runner live under `/Users/natemccoy/.claude/scripts/prioritize/`; the installed plist is `/Users/natemccoy/Library/LaunchAgents/com.natemccoy.hanadocs-prioritize.plist`.
- The daemon polls stable path/inode/size/mtime/ctime signatures of every issue file plus the goals note at a sub-second interval; it runs the semantic snapshot/scorer only after a signature changes. New, modified, renamed, and deleted issues all trigger ranking.
- The semantic snapshot covers `status`, `depends_on`, `backlog_goal`, and the four rubric fields — never the generated fields, so the watcher's own writes cannot loop. Snapshots live in `/Users/natemccoy/Library/Caches/hanadocs-prioritize/`; event logs and last-status in `/tmp/hanadocs-prioritize/`.
- Every write entry point (watcher, direct `renumber.py --apply`) holds the shared OS-released writer lock at `/tmp/hanadocs-prioritize/writer.lock`; a separate runner lock coalesces overlapping save bursts. No stale-PID cleanup.
- Body-only edits with unchanged ranking inputs are a no-op. Judgment reassessment is always an explicit `/prioritize` or `/issue` action.

### Eligibility

- Rank every issue whose `status` is `open`, at every stage (`backlog`, `active`, `waiting`).
- Closed issues keep their rubric inputs as history but lose live `backlog_score` and `backlog_rank`.

### Score model

- `A`, `I`, `U`, `E` are the star counts of `backlog_alignment`, `backlog_impact`, `backlog_urgency`, `backlog_effort`.
- Each goal's 1-based numeric prefix is `goal_position`; `goal_bonus = 2 * (goal_count - goal_position)`. Four goals produce bonuses `6`, `4`, `2`, `0`.
- Urgency scores as tiers, not a linear term: `urgency_bonus` is `0`, `2`, `4`, `20`, `70` for `U` of one through five stars.
- `backlog_score = (4 * (A - 1)) + (3 * (I - 1)) + urgency_bonus + goal_bonus - (E - 1)`
- Never divide by effort; coarse effort estimates must not let tiny tasks dominate or erase an XL strategic initiative.
- These exact weights stay explicit and centralized here.

#### Why urgency is tiered

Alignment, impact, and effort measure how valuable an issue is. Urgency measures when that value expires, which is a scheduling fact rather than a value judgment, so blending it in linearly would produce a number that is neither a good value estimate nor a good schedule.

The tiers encode where the distinction actually bites. One through three stars separate "can wait" from "feels pressing" — a judgment call that must not outrank a strategic issue, so those tiers only nudge. Four stars is heavy enough to outweigh a full alignment or impact gap on its own, but a top-scoring strategic issue can still beat a weak four-star one. Five stars is a strict override: every other term together spans 38 points, so the 50-point gap between the four- and five-star bonuses guarantees any five-star issue outranks every issue below it, whatever its other ratings.

`depends_on` still wins over urgency. Ranking sorts by score and then applies the dependency topological order, so a five-star issue with an unfinished open dependency waits behind it.

#### Keeping the override meaningful

An overruling signal invites grade inflation: it becomes the escape hatch for whatever the user wants to work on next, and once most issues sit at three or four stars the override is noise. Urgency is safe to weight this heavily only because it stays absolute and evidence-gated.

- Never re-rate urgency comparatively within a band, the way alignment and impact are re-rated. Comparative urgency is a contradiction — it hands the override to whichever issue is merely the least patient among its neighbors.
- Four and five stars require concrete, checkable time pressure named in the note: a committed date, an external dependency's cutoff, an expiring opportunity, active harm. "This feels important" or "I want to do this next" is alignment or impact, not urgency.
- Expect most issues to sit at one star permanently. A backlog where urgency is broadly distributed has a broken rubric, not an urgent backlog.

### Rubric metadata

- Store each rubric component as its own frontmatter property: YAML text scalars of one to five `⭐` characters, double-quoted on write; accept plain and single-quoted equivalents because Obsidian may rewrite them. `backlog_goal` is a scalar matching one numerically prefixed goal value (e.g. `"1 - Ship Hana"`).
- `backlog_score` and `backlog_rank` are unquoted numbers. A missing rubric property means unassessed; there is no zero-star value.
- Issue type lives in the existing `category` property, not in the rubric.

### Rubric definitions

#### `backlog_alignment` — how strongly the issue advances its selected goal (comparative within a band)

- `⭐` — weak or minimal relationship to the selected goal
- `⭐⭐` — indirectly supports the goal or removes a limited obstacle
- `⭐⭐⭐` — directly advances a meaningful part of the goal
- `⭐⭐⭐⭐` — central work on which substantial goal progress depends
- `⭐⭐⭐⭐⭐` — completing the issue itself delivers a major goal outcome; in a band, the few most goal-critical issues

Assign every eligible issue its closest `backlog_goal`, even when alignment is weak.

#### `backlog_impact` — magnitude of benefit if completed (comparative within a band; exclude urgency and effort)

- `⭐` — small or highly localized benefit
- `⭐⭐` — clear but limited benefit to a narrow workflow or audience
- `⭐⭐⭐` — significant benefit to an important workflow or audience
- `⭐⭐⭐⭐` — major benefit across a core workflow or multiple audiences
- `⭐⭐⭐⭐⭐` — transformative outcome for the product, organization, or ecosystem; in a band, the few highest-leverage issues

#### `backlog_urgency` — evidenced cost of delay (absolute; no duration estimates)

Rated against the calendar, never against the band. Four and five stars must cite concrete time pressure in the note and carry heavy scoring weight; five stars sends the issue to the top of the backlog outright.

- `⭐` — can wait; delay has little material cost. The default, and correct for most issues
- `⭐⭐` — pressure is building; delay slowly raises cost or loses opportunity
- `⭐⭐⭐` — pressing; delay materially worsens the outcome or problem
- `⭐⭐⭐⭐` — time-sensitive; an active commitment, dependency, or opportunity is at risk. Requires a named date, cutoff, or external dependency
- `⭐⭐⭐⭐⭐` — immediate; serious current harm, blockage, or a hard cutoff demands action. Overrides every other factor, so reserve it for work that must happen next

Require cited evidence for four and five stars; never invent a deadline, commitment, or closing opportunity window.

#### `backlog_effort` — relative breadth and coordination (absolute; never elapsed time)

- `⭐` — `XS`: one atomic, tightly scoped action
- `⭐⭐` — `S`: contained work with few touchpoints and a known approach
- `⭐⭐⭐` — `M`: several coordinated steps or touchpoints
- `⭐⭐⭐⭐` — `L`: broad work requiring substantial coordination or integration
- `⭐⭐⭐⭐⭐` — `XL`: a multi-phase initiative, epic, or scope too broad for one task

More effort stars mean more work, not a better issue. Flag five-star effort issues as decomposition candidates without demoting them. Never translate effort into hours, days, or weeks.

### Corrections

- An explicit user correction to any judgment field is applied immediately through the same direct-edit path, then verified against the watcher's recomputation.
- If the user disagrees with an ordering, correct the underlying rubric values — never `backlog_score` or `backlog_rank`.
