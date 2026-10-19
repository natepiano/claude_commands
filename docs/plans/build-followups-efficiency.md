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

#### As-built

- `~/.local/state/nightly-review/token-study/3-fixed/report.md` (outside git) prices what every request carries before any work, per role, for both families, against Phase 2's 7,240,515,495 weighted tokens a week.
- Largest Claude parts: other tool schemas 264,186,777 (3.65%), the Artifact tool schema 253,051,412 (3.49%; 5 calls in the week), memory index 71,478,825 (0.99%), global CLAUDE.md 40,270,186 (0.56%). All named Claude parts: 824,343,825 (11.39%). Claude reminders carried per turn: 56,277,164 (0.78%).
- Codex retained original-start blocks: 250,573,210 (3.46%), led by base instructions 103,038,176 (1.42%) and the skill catalog 43,883,789 (0.61%). This is a partial figure: Codex rollouts show no tool schemas.
- Ledger findings: none gone. The Artifact finding changed (still carried, now called); the CLAUDE.md, memory-index, context-hook and handoff findings are confirmed.
- Scripts and data: `3-fixed/claude/work/` and `3-fixed/codex/work/`.

**Files:**
- `docs/plans/build-followups-efficiency.md` — this record; the study lives outside git

**Binds later work:** no role's parts match its start size within 5%; the named residual is 15–30% of Claude start input and 21–28% of Codex start input. These figures are carried cost (estimated sizes at average input price), not savings.

**Gotchas:** a Codex thread resumed in a new rollout file must keep the retention its compactions decided (`3-fixed/codex/work/scan_rollouts.py`, checked by `check_parser.py`); restoring all start parts on resume overstated cost by 1,272,628 weighted tokens a week.

### Phase 4 — What the turns do  · status: done
**Depends on:** Phase 2, Phase 3

#### As-built

- `~/.local/state/nightly-review/token-study/4-turns/report.md` (outside git) measures, for the week 2026-10-12..19 on both machines, the turn patterns that spend tokens without moving work forward. Both halves count every request (Claude 167,627; Codex 217,204) and match Phase 2's totals exactly. Each half takes Phase 3's reserve first (Claude 879,937,859.98; Codex 250,573,210.01), so no token is counted twice.
- Disjoint patterns: 2,499,756,881.23 weighted tokens/week (34.52%). Claude carries 678,423,479.84 and Codex 1,821,333,401.39. These are exposures with a lower bound of 0, not savings.
- Largest: Codex wait-only turns 872,250,459.96 (12.05%; empty `write_stdin` 446M, sleep 270M, wait tool 126M). Then Codex repeated file reads 262,157,700.33 and the first request after compaction (Codex 268,446,616.37; Claude 99,646,720.72). Claude skill rereads are 184,706,534.08 and notification-only turns 63,541,446.27. Codex idle re-wakes come to 372,400.55, information-only peer messages to 128,065.83, and update ticks to 0.
- Scripts and data: `4-turns/claude/work/` and `4-turns/codex/work/` (`scan_patterns.py`, `check_patterns.py` with timing and replay regression checks), plus a manifest per half.

**Files:**
- `docs/plans/build-followups-efficiency.md` — this record; the study lives outside git

**Binds later work:** Phase 5 picks experiments from these exposures and prices any saving from the billed tokens of the requests it removes. The start residual (Claude 15–30%, Codex 21–28%) stays unexplained, because no tokenizer is available on natedev.

**Gotchas:** `fix_pass` is 0 on every review pass, so review rounds cannot be counted from the store. A Codex tool output enters input on the request after the call that produced it. A resumed thread replays its outputs, so they are keyed by call id and content.

### Phase 5 — Throwaway experiments  · status: done
**Depends on:** Phase 3, Phase 4

#### As-built

- `~/.local/state/nightly-review/token-study/5-experiments/report.md` (outside git) holds five offline replay experiments over the week 2026-10-12..19, one row each with before/after numbers, a weekly projection with range, a same-work check, and its own cost. Halves: `5-experiments/claude/claude.md` and `5-experiments/codex/codex.md`; setups, results and verification files are in `token-study/experiments/<name>/`.
- No live model run was made: observed savings are 0 and experiment model cost is 0. Every gain is a replay projection whose range starts at 0.
- Projections (weighted tokens/week): Codex wait-run collapse 37,638,356.80 (0.51983%; 4.32% of the wait-only exposure). Claude one pause notice per context 1,488,675.10 (the strict rule removes 0). Codex identical-read reference 13,378.65 (0–246,797.33). Codex review compaction limits: none, because no modeled limit beats observed cost and the instruction-availability check fails at every boundary.
- A request is removable from a wait run only when its tool output is timing-only and its assistant message carries no work. A message is a pause notice only when its whole body is the notice.

**Files:**
- `docs/plans/build-followups-efficiency.md` — this record; the study lives outside git

**Binds later work:** Ranked proposals label every gain "modeled", never "measured". The largest untested lever is the number of wait turns per fix/impl job (about 750M exposure).

**Ruled out:** dropping the director's `direct.md` re-Read after compaction (the copy in context holds only 260/707 and 236/710 of the required lines); raising Codex review compaction limits on cost-model evidence alone.

### Phase 6 — Ranked proposals  · status: done
**Depends on:** Phase 5

#### Work Order

**Goal:** the ranked list of ways to do the same work for fewer tokens, in the ledger and sent to the showrunner.

**Spec:**
- One proposal per change: what changes, where (file or setting, owner unit when one owns it), weekly gain as share and weighted tokens with range, evidence (phase report and query), risk to the work (what could get worse), and how to re-measure after adoption.
- Rank by gain; break ties by Simple, then Fast, then Beautiful (decision criteria).
- Each proposal's gain says which kind it is: measured saving (Phase 5), modeled estimate, or not measurable (with the reason); missing evidence is never zero.
- Fold in the open ledger token findings: each is re-measured, merged into a new proposal, marked gone, or marked inconclusive or untested with the reason (for example no Codex tool-schema evidence, a zero-match scan, a judgment not re-checked); inconclusive and untested lines stay open. A merged or gone line gets its status text updated (`superseded by <date> <title>` or `gone on re-measure <date>`); no line is deleted.
- Append each new proposal to `~/.local/state/nightly-review/ledger.md` as `- 2026-10-<dd> <scope>: <title> [gain/week: <x>% of tokens] — proposed`.
- Write `token-study/report.md`: the token map summary, the ranked table, the experiment results, and what the study did not cover.
- Send the showrunner the ranked table (top 10) and the report path.

**Files:**
- `docs/plans/build-followups-efficiency.md` — this phase's results, at shrink. Outside git: `~/.local/state/nightly-review/ledger.md` and `~/.local/state/nightly-review/token-study/report.md`.

**Workers:** nothing splits: the unit director writes the ranking and the ledger lines from the phase reports; no worker opens.

**Constraints from prior phases:** all phase reports. Phase 5 (`token-study/5-experiments/report.md`) measured no observed saving: every gain is a replay projection with range starting at 0, so label it "modeled", never "measured". Its projections (weighted/week): Codex wait-run collapse 37,638,356.80 (0.51983%, 4.32% of the 872M wait-only exposure); Claude one pause notice per context 1,488,675.10; Codex identical-read reference 13,378.65 (0–246,797.33); Codex review compaction limits none (no limit beats observed cost; instruction check fails). Do not propose dropping the director's `direct.md` re-Read after compaction (the copy in context lacks needed lines). The largest untested lever is wait turns per fix/impl job (about 750M exposure); propose it as not measurable with that reason. Phase 3–4 exposures are upper bounds, not gains. Phase 2's weighted per checkpoint is a descriptive comparison only; a claim that a change does the same work needs matched tasks or unchanged acceptance outcomes.

**Acceptance gate:** every proposal has a gain with range and evidence; the open token findings listed in Key files each have a disposition; the For you row's efficiency count equals the net count after every ledger change (new lines up, superseded or gone lines down), checked against the rendered row; the showrunner has the table.
