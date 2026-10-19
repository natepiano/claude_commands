# efficiency

> **Status: IMPLEMENTATION PLAN — phased, delegate-ready.** The "N efficiency proposals" count in the status line's `For you:` row, then a token-efficiency study of every showrunner's work that ends in measured, ranked proposals and changes nothing.

> **Production: build-followups** — unit `efficiency-unit`; production doc `docs/plans/build-followups-production.md`

## Source

2026-10-19 01:29 EDT

i need changes to the nightly efficiency investigations for code and process
1. i need the footer in  the third row to show the number of open unreviewed efficiency proposals
2. i want to stop having these run at night but instead to have them run by a single unit director called efficiency
3. it's workers can do investigations and coding
4. we should keep the proposals we currently have an d if i approve one that unit can work on them
5. it's next task should be to determine how to figure out how to reduce token usage as the highes tprioirty we have - to be able to get the same work for fewer tokens would be the highest best thing we can do.  it should not actually make any changes - but it should apply its inestigations to the work done by our other showrunners - their sessions, their turns and determine how we can improve token efficiency in general
when i say have them run - the former nigntly - i want that only on demand - we hvae a backlog that i will address with that unit on an adhoc basis but the priority right now is token efficiency investigation
it can run throwaway experiments if it chooses to

Showrunner, 2026-10-19 (relayed task order): 1. main priority: the token-efficiency study; change nothing in real work; throwaway experiments are OK; report proposals, each with its measured gain. 2. one small worker task: the status line's "For you:" row counts open efficiency proposals as one count, "N efficiency proposals": the ledger lines still proposed or deferred in `~/.local/state/nightly-review/ledger.md`. 3. the ledger backlog runs only on demand, with the user, through `/efficiency_next`; a proposal is built only after the user approves it; the nightly timers are gone.

## Delegation Context

- **Project:** `~/.claude` config repo. The unit director names the worktree and branch git reports at dispatch in every Work Order.
- **Project started:** 2026-10-19T08:52:48.640+00:00
- **Stack:** Python 3 standard library, checked by basedpyright; SQLite (the usage ledger); Claude Code transcripts (JSONL) and Codex rollouts (JSONL).
- **Layout:** Phase 1 edits `scripts/production/` and `scripts/statusline/` tests, `docs/production_format.md`. Phases 2–6 edit no file in the repo except this plan. Their scripts and reports go in the study folder `~/.local/state/nightly-review/token-study/` (outside git; the unit owns `~/.local/state/nightly-review/` per the production doc's Units row).
- **Key files:**
  - `scripts/production/footer_counts.py` — the counts line. `FooterCounts(changes, proposals, projects, questions)` NamedTuple; `read_counts(doc, *, questions)`; `sources(doc)` lists the files the status line cache watches; `counts_line(counts)` renders `For you: 2 changes to review, 1 proposal, 3 projects, 1 question`, zero counts left out. `ledger(doc)` reads the `Improvement queue` field through `production_units.production_field(lines, name)`, which raises `Refusal` when the field is absent. `total(each)` sums by column.
  - `scripts/production/test_footer_counts.py` — builds `FooterCounts` positionally (`FooterCounts(2, 1, 2, 1)`), so a new field goes last.
  - `scripts/hooks/showrunner_footer.py` `counts_line(productions, questions)` — totals `read_counts` over the productions, then renders. `scripts/statusline/counts_row.py` writes the cache from it and from `footer_counts.sources`. `scripts/production/dailies_render.py:1705` renders the same line in the dailies footer. None of these three need edits: they pass `FooterCounts` through.
  - `scripts/statusline/test_statusline.py:590` — the counts-row end-to-end tests (a doc with `- **Improvement queue:** <ledger>`).
  - `docs/production_format.md` — documents the Production Context fields; `Improvement queue` is the model.
  - `~/.local/state/nightly-review/ledger.md` — the efficiency ledger. One line per proposal: `- <date> <scope>: <title> — <status>[; from …]`. The status is the text after the **first** ` — `; later ` — ` segments belong to `from Also found:` sub-items. At planning (04:49 EDT 2026-10-19): 124 `proposed`, 6 `deferred`, 13 `accepted`, 2 `declined`, 1 `rejected`, 1 `shown`, 5 `no proposal`; open = 130.
  - `docs/as-built/usage-ledger.md` — the usage store `~/.local/state/usage-model/usage.db`: every model request on both machines, Claude and Codex, with token classes, model, effort, role (`showrunner`, `director`, `impl`, `test`, `fix`, `review`, `worker`, `interactive`, `subagent`), unit, activity (`wait`, `status`, …), compactions, checkpoints, findings, review passes. `python3 scripts/usage/usage_report.py list` prints the metrics (`weighted`, `weighted_per_checkpoint`, `before_first_edit_share`, `cache_read_share`, `output_share`, `compactions_per_thread`, `compaction_reread_share`, `weighted_per_finding_closed`, `weighted_per_review_round`, `wait_share`, `status_share`, `context_share`); `--by role,model,unit,machine,day,activity,effort,lens`, `--since/--until`. `scripts/usage/weights.py` holds the per-model weights. `python3 -m scripts.usage.compaction_replay` replays Codex threads under other compaction limits. `scripts/usage/collect.py` adds new turns (one pass at a time, a lock file).
  - Raw sources: Claude transcripts `~/.claude/projects/**.jsonl` (21 GB) with `subagents/agent-*.jsonl`; Codex rollouts `~/.codex/sessions/**` (22 GB). The Mac's are on the Mac (`ssh mac`); the store already holds their token counts.
  - Productions (`showrunners.py list`): `build-followups` (natedev, `docs/plans/build-followups-production.md`), `cargo-liner-style` (`~/rust/cargo-liner-style/docs/cargo-liner-style-production.md`), `remove-running-section` (cargo-liner, `~/rust/cargo-liner/docs/cargo-port/remove-running-section-production.md`), `tool-based-ui` (hana, `~/rust/hana_catalyst/docs/hana/tool-based-ui-production.md`). Each doc's `## Units` table maps a worktree to a unit; `usage_report.py --production-docs` reads them for the `unit` dimension.
  - Earlier token findings, open in the ledger, that the study re-measures instead of finding again: handoff instruction on every tool call (10-01); oldest unit directors fed pre-10-01 skill text (10-06); `delegate.md` spine after compaction, 2.2% (10-07); Artifact tool 10.5k tokens in 98% of requests, 5.0% (10-12); update ticks 24% of showrunner input (10-01); `/build_hold` broadcast 24.2M a week (10-06); `/unit:direct` compaction re-read 8.6% / 4.8% (10-13); `/unit:direct` in context twice after compaction, 1.35% (10-16); showrunners lose 73% of `/showrunner:produce` after compaction (10-16); CLAUDE.md false claims and repeats (10-15); stale memory index lines, 0.2% (10-13, 10-17); context-usage hook repeats after 80%, 0.34% (10-18); CLAUDE.md loaded twice in `claude-build-followups-*` worktrees, 0.32% (10-19). Their methods and scripts are in `~/.local/state/nightly-review/<date>/` (`config.md`, `digest.md`, `work/`).
- **Test lanes:** Phase 1: `scripts/production/test_footer_counts.py`, `scripts/statusline/test_statusline.py`. Phases 2–6: none (study scripts are throwaway).
- **Build:** none (Python).
- **Test:** `HERDR_BIN=/nonexistent/herdr python3 scripts/production/merge_gate.py --repo <tree> --tests-only` from the repo root (`<tree>` may be a `git archive` export in the scratchpad).
- **Lint:** `basedpyright scripts/production scripts/statusline` — pass is its `0 errors, 0 warnings` line.
- **Invariants:**
  - The study changes nothing in real work (user, 2026-10-19: "it should not actually make any changes"). No edit to `~/.claude` main, `settings.json`, skills, commands, CLAUDE.md, memory, hooks, other productions' repos or docs; no message to any session but this unit's showrunner. Reads of transcripts, rollouts and the usage store are free; one `collect.py` pass to bring the store current is allowed (it is the store's normal writer).
  - Throwaway experiments are allowed (user, 2026-10-19). Each runs in a scratch config home or scratch worktree under `~/.local/state/nightly-review/token-study/experiments/`, never under a real session's config dir, and its own token cost is recorded with its result. Budget (unit director's call): at most 3M weighted tokens across all experiments; an experiment that would pass it goes to the showrunner first.
  - Every number in a report names the query or script that made it, its window and its machine coverage (the `Sources` block of `usage_report.py`). Gains are stated per week, as a share of all weighted tokens and in weighted tokens, with a low–high range.
  - The default window is the 7 days `--since 2026-10-12 --until 2026-10-19` in America/New_York, both machines, both families. A phase that needs another window says why.

## Phases

### Phase 1 — "N efficiency proposals" in the For you row  · status: done
**Depends on:** none

#### As-built

- `FooterCounts` ends with `efficiency: int = 0`. `read_counts(doc, *, questions)` fills it from the doc's `- **Efficiency ledger:** <path>` line; `sources(doc)` lists that path even before the file exists, so the status line cache refreshes when the ledger appears or changes.
- `efficiency(doc)` counts dated ledger lines (`- YYYY-MM-DD … — <status>`) whose status, the text after the first ` — `, starts with `proposed` or `deferred`. An absent field, a missing file, or a file that cannot be read or decoded counts 0 and leaves the other counts in place.
- `counts_line` renders `plural(counts.efficiency, "efficiency proposal")` right after the proposals count: `For you: 1 change to review, 2 proposals, 130 efficiency proposals, 3 projects, 1 question`.
- `docs/production_format.md` documents the `Improvement queue` and `Efficiency ledger` fields.

**Files:**
- `scripts/production/footer_counts.py` — the For you counts, their sources, and the line
- `scripts/production/test_footer_counts.py` — parser, missing and undecodable ledger, render order
- `scripts/statusline/test_statusline.py` — end-to-end efficiency count in the status line
- `docs/production_format.md` — the two ledger fields

**Binds later work:** each `proposed` line Ranked proposals appends to the efficiency ledger raises the For you count by one.

### Phase 2 — Where the tokens go  · status: done
**Depends on:** none

#### As-built

- `~/.local/state/nightly-review/token-study/2-map/report.md` (outside git) maps the week 2026-10-12..19, both machines, both families: 7,240,515,495 weighted tokens, Claude 56.1% and Codex 43.9%, by role, model, production, unit, machine, day and token class, with shares by role and the top 10 (production, role, class) sinks. Cache reads are 60% of all cost.
- Yardsticks: 706 checkpoints counted from the git history of every production repository (10,255,688 weighted each; build-followups 4,444,459, tool-based-ui 24,478,002), 2,114 findings closed (3,425,031 each), 1,136 review rounds (6,373,693 each).
- Scripts and data: `2-map/claude/work/` (Claude cells, `cells.jsonl`) and `2-map/codex/work/` (Codex cells, `cube.csv`; checkpoints, `git_checkpoints.py`).

**Files:**
- `docs/plans/build-followups-efficiency.md` — this record; the study lives outside git

**Binds later work:** every later gain is stated against these role and production totals and the git-based per-checkpoint yardstick. Codex fix, impl and test workers spend 27–36% of weighted tokens on waiting turns; reviewers 27% on compaction rereads.

**Gotchas:** the usage store holds only `.claude` checkpoints, so checkpoints come from each production repository's git history. Codex logs record no cache writes: Claude cache write and Codex uncached input are the same kind of cost.

### Phase 3 — The fixed cost of every request  · status: done
**Depends on:** Phase 2

#### Work Order

**Goal:** for each role, the tokens every request carries before any work (system prompt, tool schemas, deferred-tool and skill listings, MCP instructions, CLAUDE.md, memory index, session-start hook output), each part sized and multiplied by its weekly request count.

**Spec:**
- Claude: the first request of fresh sessions per role gives the start size (cache write). Size each part from its source text (CLAUDE.md files, `MEMORY.md` per project, `skillOverrides`, the skill listing, MCP server instructions, deferred tool list, SessionStart hook output) and check the parts against the start size; the gap is the system prompt and tool schemas. Reuse the methods of the 10-12 Artifact-tool and 10-15/10-19 CLAUDE.md findings.
- Per-request repeats: system reminders injected each turn (hooks, cross-session messages, task notifications, context-usage lines), counted from transcripts by kind, with tokens per week.
- Codex: the worker start prompt (`AGENTS.md`, the Work Order, Delegation Context, review prompts by lens) and its size per thread start.
- Re-measure the open ledger findings that fall here (Artifact tool, CLAUDE.md twice and its repeats, memory index lines, context-usage hook repeats, handoff instruction) on this window; mark each as confirmed, changed or gone.
- Output: a table part → tokens per request → requests per week → weighted tokens per week → share; `token-study/3-fixed/report.md`.

**Files:**
- `docs/plans/build-followups-efficiency.md` — this phase's results, at shrink. Nothing else in the repo; the work goes in `~/.local/state/nightly-review/token-study/3-fixed/` (outside git).

**Workers:** 2 writers (no test lane).
- `impl` — Claude fixed parts and per-turn reminders.
- `test` opens as `impl` — Codex start prompts and review prompts.

**Constraints from prior phases:** the role and production totals and token-class split from "Where the tokens go" are the denominators.

**Acceptance gate:** the parts of each role's start size add up to the measured start size within 5%, or the report names the unexplained part; the shares use Phase 2's totals; Invariants checks as Phase 2.

### Phase 4 — What the turns do  · status: todo
**Depends on:** Phase 2, Phase 3

#### Work Order

**Goal:** the work patterns that spend tokens without moving work forward, across all four productions, each with tokens per week and the sessions that show it.

**Spec:**
- Sample per production: the showrunner session and the two most expensive unit directors and two Codex workers of the window (Phase 2's sinks), read from transcripts and rollouts.
- Measure, each as weighted tokens per week and share:
  - turns with no new work: waits, status checks, polls, update ticks, idle re-wakes (`activity` dimension plus transcript reading);
  - context growth: requests per session before compaction, context size at each request, compaction re-reads (re-measure the 10-13 and 10-16 `/unit:direct` and `/showrunner:produce` findings);
  - large tool results kept in context (file reads, build and test logs, `git` output), and the same file read more than once in one context;
  - messaging between sessions: showrunner ↔ unit relays, broadcasts (re-measure `/build_hold` 24.2M), reviewer round trips;
  - review and repair loops: rounds per finding, rounds that did not converge;
  - model and effort per role against the work it did (an Opus turn that only relays).
- Compare productions on `weighted_per_checkpoint`; explain the spread.
- Output: one row per pattern with evidence (session ids, turn counts), `token-study/4-turns/report.md`.

**Files:**
- `docs/plans/build-followups-efficiency.md` — this phase's results, at shrink. Nothing else in the repo; the work goes in `~/.local/state/nightly-review/token-study/4-turns/` (outside git).

**Workers:** 2 writers (no test lane).
- `impl` — Claude sessions (showrunners, directors, subagents).
- `test` opens as `impl` — Codex workers and reviewers.

**Constraints from prior phases:** Phase 2's sinks pick the sample; Phase 3's fixed cost per request prices each avoided request.

**Acceptance gate:** each pattern names at least two sessions as evidence and its weekly tokens; the patterns do not count one token twice with Phase 3 (the report says how overlap was removed); Invariants checks as Phase 2.

### Phase 5 — Throwaway experiments  · status: todo
**Depends on:** Phase 3, Phase 4

#### Work Order

**Goal:** a measured gain for each candidate from Phases 3 and 4 whose estimate rests on an assumption a test can settle.

**Spec:**
- Pick at most five candidates by expected weekly tokens × uncertainty. Each experiment states its question, setup, the number it reads and its own token cost.
- Allowed setups: a scratch `CLAUDE_CONFIG_DIR` copy with one change, a headless `claude -p` run on a fixed prompt read from a real session's first request (start size with and without the change); `compaction_replay` under other limits; a replay script over stored turns; a Codex thread on a scratch worktree with a shortened start prompt. Never a real session, account move, or edit to live config.
- Budget per Invariants (3M weighted tokens in total).
- Output: one row per experiment: change, measured saving per request or per thread, scaled to weekly tokens with a range; `token-study/5-experiments/report.md`; setups kept under `token-study/experiments/`.

**Files:**
- `docs/plans/build-followups-efficiency.md` — this phase's results, at shrink. Nothing else in the repo; the work goes in `~/.local/state/nightly-review/token-study/5-experiments/` and `token-study/experiments/` (outside git).

**Workers:** 2 writers (no test lane), one experiment set each; the unit director assigns experiments by family.

**Constraints from prior phases:** the candidates and their estimated gains come from "The fixed cost of every request" and "What the turns do".

**Acceptance gate:** each experiment reports before/after numbers, its cost and a pass/fail against its estimate; total cost under budget; Invariants checks as Phase 2.

### Phase 6 — Ranked proposals  · status: todo
**Depends on:** Phase 5

#### Work Order

**Goal:** the ranked list of ways to do the same work for fewer tokens, in the ledger and sent to the showrunner.

**Spec:**
- One proposal per change: what changes, where (file or setting, owner unit when one owns it), weekly gain as share and weighted tokens with range, evidence (phase report and query), risk to the work (what could get worse), and how to re-measure after adoption.
- Rank by gain; break ties by Simple, then Fast, then Beautiful (decision criteria).
- Fold in the open ledger token findings: each is re-measured, merged into a new proposal, or marked gone. A merged or gone line gets its status text updated (`superseded by <date> <title>` or `gone on re-measure <date>`); no line is deleted.
- Append each new proposal to `~/.local/state/nightly-review/ledger.md` as `- 2026-10-<dd> <scope>: <title> [gain/week: <x>% of tokens] — proposed`.
- Write `token-study/report.md`: the token map summary, the ranked table, the experiment results, and what the study did not cover.
- Send the showrunner the ranked table (top 10) and the report path.

**Files:**
- `docs/plans/build-followups-efficiency.md` — this phase's results, at shrink. Outside git: `~/.local/state/nightly-review/ledger.md` and `~/.local/state/nightly-review/token-study/report.md`.

**Workers:** nothing splits: the unit director writes the ranking and the ledger lines from the phase reports; no worker opens.

**Constraints from prior phases:** all phase reports; the "same work" yardstick is Phase 2's weighted per checkpoint.

**Acceptance gate:** every proposal has a gain with range and evidence; the open token findings listed in Key files each have a disposition; the For you row's efficiency count changes by the new lines; the showrunner has the table.
