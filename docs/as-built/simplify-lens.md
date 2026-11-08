# Daily simplify review

## What it is

The daily simplify run reviews all changes since the last successful run on the codebase.
The first unit that finishes a phase each day starts the run after its code ready notice.
The next phase does not wait for it.
Phase reviews use `adversary`, `contract`, and armed `rerun` lenses without simplify.

The run uses four Claude agents: `reuse`, `simplification`, `efficiency`, and `altitude`.
They report proposed changes without editing files.
The lens runs on the Claude account of the unit director that starts it through the caller's `CLAUDE_CONFIG_DIR`.
Codex fix workers apply the repairs.

## How it works

### Files and launch

| File | Role |
| --- | --- |
| `docs/direct/phase_end.md` | Starts the daily run after the code ready notice. |
| `docs/direct/repair_rounds.md` | Assigns findings to a repair phase and records decisions. |
| `scripts/direct/assign.py` | Sends `--role simplify` to the daily launcher. |
| `scripts/direct/simplify_daily.py` | Claims the day, saves the diff, starts Claude, and updates the last successful tip. |
| `scripts/direct/simplify_result.py` | Prepares the prompt and checks the report and agent results. |
| `scripts/copies/copies.py` | Creates the task clone that the reviewer reads. |
| `scripts/agents/agent_exec.sh` | Starts Claude in plan mode without the three editing tools. |
| `config/direct.conf` | Sets `SIMPLIFY_LENS`, `SIMPLIFY_FIRST_RUN_BEFORE`, and `SIMPLIFY_AGENTS`. |
| `config/models.conf` | Selects `direct.simplify=claude` and `simplify=opus:high`. |
| `config/review/simplify.md` | Defines the diff scope and report rules. |
| `scripts/direct/findings.py` | Stores findings, held items, answers, and closure verdicts. |

The director starts this command as a background shell:

```sh
bash ~/.claude/scripts/direct/assign.sh --role simplify --session-dir "${SESSION_DIR}" --cwd "${WORKING_DIR}"
```

This role takes no phase, prompt, lens, or pass.
`SIMPLIFY_LENS=off` prints `simplify: off` before Git or Claude starts.

### Scope and claim

The launcher finds origin's default branch with `git ls-remote --symref origin HEAD`.
It fetches that branch and `refs/simplify/*` from origin.
The fetched branch tip supplies `tip`; the unit worktree's `HEAD` does not select the review scope.

`refs/simplify/last` supplies `base` when it exists.
The first run selects the last commit before `SIMPLIFY_FIRST_RUN_BEFORE` on the default branch.
The configured time is `2026-11-05T22:50:00-05:00`.
Without an earlier commit, the repository's root commit supplies `base`.
An empty `git diff <base> <tip>` prints `simplify: no changes` and creates no claim.

The date uses the user's zone from `scripts/lib/user_zone.py`, which also supplies `scripts/lib/zone.py`.
The launcher pushes `tip` to `refs/simplify/claim/<date>` with an empty expected value in `--force-with-lease`.
The claim permits one run per repository each day across units and machines.
An existing claim prints `simplify: claimed today` and starts no Claude.
Other push failures print Git's reason and return 1.
After a successful claim, the launcher deletes origin's claim refs for earlier days.
A failed delete prints one line and does not stop the review.

### Review and result files

The launcher calls `copies.clone` and checks out `tip` detached in that clone.
The reviewer reads the clone, not the unit worktree.
The launcher removes only its own task clone by path when the review ends.

The files live under `<run>/simplify/<date>/`:

| Path | Contents |
| --- | --- |
| `diff` | The saved diff from `base` to `tip`. |
| `source_prompt.md` | The repository, both commits, and the user's scope words. |
| `prompt.md` | `/simplify <absolute diff path>`, standing instructions, and the source prompt. |
| `report.txt` | `Pass` or numbered findings, followed by each agent's result; errors start with `ERROR:`. |
| `agent.log` | The Claude event stream and launch details. |
| `status` | One atomic line: `preparing`, `reviewing`, `reviewed`, or `error`. |

The launcher owns the report, status, and log paths.
The result module prepares the prompt and validates the report as a library.
The launcher records a fresh session UUID as role `review`, lens `simplify`, in `agents.jsonl`.
A failed usage record prints one error line and does not stop the review.
It passes that UUID to Claude with `--session-id`.

`agent_exec.sh` uses `--print --permission-mode plan --disallowedTools Edit,Write,NotebookEdit`.
It keeps `Agent` for the four review agents.
`CLAUDE_CODE_DISABLE_BACKGROUND_TASKS=1` disables background tasks.
The prompt requires foreground Agent calls and all four results before the final report.

A failed prompt preparation writes status `error` and an `ERROR:` report before Claude starts.

The result checker permits a heading or preamble before `Pass` or item 1.
It rejects empty reports, Claude errors, and reports without that result form.
When Claude supplies agent statistics, all configured agents must complete without failure.

After a successful review, the launcher writes status `reviewed` before it pushes `tip` to `refs/simplify/last`.
The push uses a lease for the previous value.
A failed status write or ref push sets status `error`.
The ref stays unchanged, so the failed run's changes remain in scope for the next day's run.
The launcher prints `simplify: <status>; report <path>` and returns 0 for `reviewed`, or 1 for `error`.
Daily runs have no separate simplify fields in the phase review-trial line.

### Findings and decisions

The director that started the run adds a phase `simplify-<date>` to its plan from the report.
It records every item in that phase's ledger with `--lens simplify` and discards none.
An in-scope item preserves behavior and stays within the saved diff.
In-scope items and items that remove a copy enter one Codex repair phase through the normal checkpoint.
Other behavior changes and changes outside the diff use `--held-for-decision`.
Items that add a copy require a decision.

`findings.py` stores held items as `held_for_decision` outside repair batches.
Held items do not block the phase gate and remain in the ledger after the phase ends.
The checkpoint notice lists their ids, files, and proposed changes.

Only these held items reach the user:

1. Changes that affect money.
2. Changes that affect safety.
3. Changes that affect product behavior the user sees.
4. Acts that cannot be undone.

The director settles every other held item under `DecisionEconomy` and states the decision in one line.
The showrunner files user questions for the listed categories.
Approved work returns to the owning unit as a `next` item or a new phase.

`decision --id F001 --answer approved|declined --user-words "<answer>"` records the user's own words.
It sets the state to `decided` and applies no code change.
A changed ruling requires `--withdraw "<reason>"`.
The old decision enters `decision_history` with `withdrawn_epoch` and `withdraw_reason`.
The new `finding_decision` event includes that old object as `withdrew`.
Repeating the same answer and words preserves the saved decision.
The command can record an answer after the phase closes.

An approved decision permits a normal repair verdict after the repair exists.
Approval alone does not accept the repair.
Held items and declined decisions refuse closure verdicts.

## Invariants

1. Keep all four agents and the `Agent` tool.
2. Keep plan mode and remove `Edit`, `Write`, and `NotebookEdit`.
3. Review the saved diff against origin's default branch tip.
4. Claim one run per day on origin before Claude starts.
5. Move `refs/simplify/last` only after a successful review.
6. Record every finding and preserve held items and withdrawn decisions.
7. Remove only the run's own task clone.

## Calibration / gotchas

A failed run keeps its daily claim and does not advance the last successful tip.
Another unit on that day does not retry the review.
The next day's run includes the failed scope.
A successful no-change check creates no claim, so a later checkpoint that day can start a run.
A stale lease during the final ref update reports an error.

`SIMPLIFY_AGENTS` supplies both the prompt names and the expected completed-agent count.
A model or Claude output change can affect result extraction.
A held finding requires the `simplify` lens; other lenses cannot use the held state.

## Why

The repository claim limits Claude review cost across units.
The last successful tip keeps the scope complete across failed runs.
The clone separates the review from active work.
The ledger preserves every proposed change and reserves user decisions for the four listed categories.
