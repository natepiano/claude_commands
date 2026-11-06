---
description: Send a small unit fix from its worktree.
---

# Fast-track a small fix

<FastTrack>
A unit director may write and send a small fix from its own `~/.claude`
worktree with:

```sh
~/.claude/scripts/lib/py ~/.claude/scripts/production/fast_track.py --unit <unit> --production <showrunner name> --why "<one line>" <path>...
```

Use the parent showrunner row's `name` for `--production`.
Read the doc from that row's `doc.path`. Its `Promote:` field names the installation targets. Name only the fix's files, including new files. The
command takes their changes against HEAD; it refuses paths outside the repo,
no change, conflicts with main, and the `refused_paths` in
`config/fast_track.conf`; Python files that import a refused repo module, directly or through other repo modules, are also refused.

That file alone sets `max_changed_lines`, `max_files`,
`check_time_limit_seconds`, `max_push_attempts`, `fetch_lock_attempts`, and
`fetch_lock_wait_seconds`. A missing or invalid key
refuses the request. Use the normal phase path when a limit is exceeded.

The command applies the patch in a shared clone of origin/main. It runs each
changed Python file's direct test modules, package import and basedpyright,
without the merge gate or a build queue. It pushes main fast-forward only,
then installs that commit through the production's promotion targets. A moved
main causes a new apply and check before another push attempt. A fetch of
origin main that fails with `cannot lock ref`, for example during a checkpoint
merge, waits `fetch_lock_wait_seconds` and fetches again.

After push, the named worktree paths return to HEAD; new files are removed.
The next merge of the merge branch brings the fix back. A promotion failure
keeps the push and reports exit 1. Each attempt retains its own publication,
restore and promotion state. Repeat the command to finish safely; later edits
are kept. An empty request after recovery prints `already on main at <commit>`;
new edits still run the fast check and push. HEAD containing the pushed commit
closes its restore. Paths overlap when equal or when one contains the other;
any overlap validates the attempt's whole original snapshot. Unrelated restore
or promotion failures print a warning and leave that attempt pending.

For an unsafe restore, run `fast_track.py --unit <unit> --close-restore <attempt>`
with the attempt ID from the refusal. This permanently retires automatic
restore, including after a cherry-pick or squash, without changing files,
the index, publication, promotion or pending review. Repeating it is safe.
Review discovery recovers pushed commits even without a command retry; unpublished attempts
whose base moved are closed.

Each push stays pending in `~/.local/state/fast-track/records.jsonl`;
`fast_track.py --pending --unit <unit>` prints the pending diffs. The unit's
next broad blind review receives its diff automatically and marks it reviewed
with that pass. Findings use the normal ledger and repair path. The next merge
gate takes main and tests the fix through the standard process. Neither
follow-up is optional. The nightly full suite also tests it: on natedev,
`claude-nightly.timer` runs `claude-nightly.service` on origin/main at
03:00, and a red run messages the sdlc showrunner.
</FastTrack>
