---
description: Query stage, phase, and run timings across skill run history
---

# history

`$ARGUMENTS` — a subcommand (`summary`, `compare`, `stages`, `phases`, `runs`,
`tests`, `versions`) and its flags.

Run:

```bash
python3 ~/.claude/scripts/history/history.py $ARGUMENTS
```

Empty `$ARGUMENTS` → run `summary`. A first word that is not one of the seven
subcommands → show `history.py --help` and stop.

**Runs sandboxed — never pass `dangerouslyDisableSandbox`.** The script reads
`~/.local/state/*/runs/*.jsonl` and writes nothing. Sibling commands disable the
sandbox because they rewrite conf files; this one has no reason to.

## Exit codes

Exit 1 does not always mean failure — check what was printed before reporting:

- **An empty table** — nothing matched the filters. Report no matching data; this is
  a result, not an error.
- **`No history store named …`** — a wrong `--skill`. The message lists the real
  stores; retry with one of them.
- **Anything else non-zero** — stop; do not invent a correction.

Relay stderr `note:` lines exactly.

## Output

Every subcommand prints a wide fixed-width table — header row, dashed rule, data
rows.

**Always render it as a markdown table** — never relay the fixed-width form raw. Use
the script's own header row as the columns, in its order, and drop the dashed rule.
Trailing summary lines follow the table as plain text.

`--format json` emits `{"_meta": {...}, "rows": [...]}` and `--format csv` appends
`_meta,<key>,<value>` rows; both exist for piping — relay that output verbatim,
unconverted.

## Subcommands

- `summary` — start here. Time per stage, plus the phases holding the fix tail,
  and the three commands worth running next
- `compare` — one change, the window before it against the window after, with a
  verdict per row. Needs `--on <date>`
- `stages` — stage timings, grouped by `--group` (default `stage,agent`); duplicate
  keys are rejected
- `phases` — review and fix rounds per phase, grouped by `(run, phase)`; columns
  `run  phase  reviews  fixes  impl  fix  total`
- `runs` — one row per run
- `tests` — verification and smoke history
- `versions` — which spine and schema versions each store holds

Every subcommand takes `--skill`, `--since` (`30d`, `12h`, `3M`, or an ISO date;
units are case-sensitive, `m` minutes and `M` months), and `--format
table|json|csv`. Beyond those:

- `--status`, default `completed`, on `stages`, `phases`, `runs`, `tests` — a comma
  list or `all`. **The default excludes canceled, error, and interrupted rows**, so
  every total is smaller than an unfiltered read of the same corpus. Pass
  `--status all` to include them.
- `--stage`, `--label`, `--agent` substring filters on `stages` and `runs`
- `--group slot` — which seat of the phase team (`impl`, `impl2`, `test`,
  `review`) recorded the pass. **Empty on every pass before 2026-08-31**, and on
  any recorder outside `implement.sh`, where it groups as `-`. A seat, not a
  role: a seat's role changes within a run — all three implementing, then all
  three testing, any mix — so this names the chair, and `stage` names the work.
  It does not yet make a team phase visible. The recorder can now hold one open
  pass per seat, but the orchestrator still hands `pass_kind` to a single
  member, so `slot` names which member that was and the other two stay
  uncounted
- `tests` — `--label`, `--agent`, `--raw` (it pins `stage=test` itself)
- `phases` — `--sort fix_passes|total`, default `total`
- `compare` — `--on <date>` (required), `--window` (default `14d`, the span taken
  either side), `--group` (default `agent`), and the same stage/label/agent
  filters. It ignores `--since`: the two windows are the only time filter, and a
  third would silently empty one of them
- `versions` — nothing else, `--status` included

### Reading a `compare` verdict

- **`one window only`** — the row ran on one side of the date and not the other.
  Consecutive eras, not competitors; whatever else changed between them is folded
  into any difference. This is the most common wrong conclusion the tool prevents.
- **`unchanged`** — the medians are under 1.5x apart, which rerunning the same
  configuration can produce on its own.
- **`N.Nx faster|slower (thin)`** — a real gap, but under 80 samples on one side.
  Directional, not settled.
- **`N.Nx faster|slower`** — enough samples, and past the noise factor.

**`--skill plan-delegate`** — the value is the store directory name under
`~/.local/state/`, not the user-facing skill name. Every visible name says
"delegate" (`/plan:delegate`, `[delegate.codex]`), but `--skill delegate` matches no
store. `plan-delegate` is the only one.

## Reading

- **Compare agent:effort with `stages --group stage,task,agent`.** `called_agent`
  and `called_task` are stamped on every pass event, so the grouping reads the
  assignment off the data instead of reconstructing it.
- **`task` and `stage` say the same thing for a delegate pass**, so group on one
  or the other, not both, unless a second skill is in the window. Delegate tasks
  are the four kinds — `impl`, `test`, `fix`, `review` — and `agents.conf` holds
  one row per kind. It used to hold five keys where three (`architect`,
  `mechanical`, `escalation`) named an agent tier rather than a job, and on
  2026-08-30 the tier axis was removed and 6141 stored events were rewritten to
  the kind each pass had actually run under. **Nothing is translated at read
  time**, so a query over any window returns the four; the pre-migration logs
  are kept at `~/.local/state/plan-delegate/backup-runs-2026-08-30-four-kinds`
  and are the only place escalated work is separable from ordinary work.
- **Stage totals are agent time, not elapsed.** The footer prints both and their
  ratio (about 1.7x parallel); summed stage seconds exceed the elapsed union.
- **A cell needs roughly 80 samples** before a difference under 1.5x separates from
  resampling noise. The `stages` footer repeats this; report `n` beside every median.
- **Add `worktree` to the grouping for a repo-level claim.** Implementation
  log-variance is 30.3% between worktrees against 16.3% between phases — the repo
  explains more of a timing difference than phase size does.
- **The `test` stage holds two different things, so split it before reading a
  median.** Verification runs (activity labels `verify`, `smoke`) are machine time
  with a 14s median; a test seat authoring tests is agent time in the tens of
  minutes. Both are testing, which is why both bucket to `test` — but one median
  over the pair describes neither. `--group stage,label` separates them: a pass row
  carries its kind as its label, an activity row carries its activity name.
- **`phases` counting fix rounds is the metric with power**: one fix round off the
  median phase is about -19% of phase time.
- **Fix-round count is a proxy.** It falls when implementers get more careful and
  equally when reviewers get lazier, so read it beside a quality signal, never alone.
- **`phases` `reviews` counts rounds run, not rows recorded.** An early review
  disarmed as `adopted` folds its head start into that review pass instead of
  emitting its own row.
- **`phases` `total` spans every stage in the phase** — review, test and final
  included — so it far exceeds `impl + fix`. There is no findings column:
  `finding_opened` events carry no `phase_id`.
- **`runs`: a `wall` value ending in `+`** (`39h24m+`) is last-event-minus-start for
  a run that never closed, not a duration. Its `stages` column is summed seconds, a
  duration; `n` is the row count.
- **`tests`** sorts results into pass/fail/other on a word-boundary match; `--raw`
  groups by the result text instead, one row per distinct wording.

## Versioning

Three stamped fields on every event: `spine_version` (the contract `history.py`
reads), `skill` (which skill emitted the line), `schema_version` (that skill's own
event schema).

To instrument a second skill, append JSON objects, one per line, to
`~/.local/state/<skill>/runs/<run_id>.jsonl`. It appears in `history.py` with no
registration.

`stage_finished` requires `skill`, `spine_version`, `schema_version`, `run_id`,
`timestamp_epoch`, `stage` (`implementation` | `review` | `fix` | `test` | `final` |
`other`), `label`, `elapsed_seconds`, and `started_at`. Optional: `task`, `result`,
`status`, `branch`, `worktree`, `phase_id`.

- **`agent` is a nested object** — `{family, model, effort}`, not a string. An empty
  `model` blanks the agent column; `effort: "unset"` renders as bare `family/model`.
- **`skill` must equal the store directory name**, and that is the value `--skill`
  matches. A mismatch leaves those events unreachable by name.
- **The wall-clock column needs two things**: `run_started_at` on the run's events,
  and a separate `run_finished` event carrying `run_elapsed_seconds`. Without both,
  the run reads as open and its `wall` gets the `+` suffix.
- **Emit `status`** on anything that can end badly; the default `--status completed`
  is what keeps abandoned work out of every total.
- A store outside `~/.local/state/plan-delegate` needs its own `writePaths` entry in
  `settings.json`, which takes a restart.

## Examples

```text
/history                                                  stage timings, last 14 days
/history stages --group stage,task,agent                  compare agent:effort per stage
/history stages --group stage,worktree --since 30d        where the repo, not the phase, moved the time
/history stages --status all --since 30d                  include canceled and interrupted rows
/history phases --skill plan-delegate --sort fix_passes   phases ranked by fix rounds
/history runs --since 7d                                  one row per run
/history tests --raw                                      verification results, one row per wording
/history versions                                         what each store holds
```
