---
description: Read-only review with one proposal per mode, run on demand on natedev in the order config, rust, messages.
argument-hint: config | rust | messages | config rust messages
---

Modes: `$ARGUMENTS`, done one at a time in the order given. Finish a mode, its report written and its message sent, before starting the next; a mode that cannot finish says why in its report, and the next still runs. NIGHT is the date the night's job message names (`NIGHT=<date>`), or the local date today when the command is run by hand, DIR is `~/.local/state/nightly-review/NIGHT`, LEDGER is `~/.local/state/nightly-review/ledger.md`.

<Rules>
- Read-only: write nothing outside DIR except `review.jsonl` dry sample records. No edits, commits, pushes, rebuilds or installs. A worktree only when a measurement needs a build that must not touch a working copy; remove it after.
- Start research subagents when a candidate needs more evidence than you can gather directly.
- Nobody is watching. A denied or failed action is a finding, not a blocker: note it and continue.
- Read LEDGER first. A proposal marked declined comes back only with new evidence, named as such.
</Rules>

<Scope mode="config">
Every memory (`~/.claude/projects/*/memory/`; the Mac's over `ssh mac` if it answers, else say it was skipped), `~/.claude` commands, skills, docs, scripts and settings, and `/etc/nixos` for both platforms. Look for duplication, rules that contradict each other, stale memories, configuration that could be simpler, and friction that recurs in `/etc/nixos/docs/inbox/natedev.md` and `docs/status.md`.
</Scope>

<Scope mode="rust">
Choose one target with fresh evidence: hana, cargo-liner, bevy_brp, obsidian_knife, nateroids, or showrunner.
- A project: correctness, performance, tooling, and style rules that cost more than they catch. Leave code style sweeps to its style production.
- `showrunner`: the showrunner, producer and unit commands under `~/.claude/commands/` with their docs and scripts, against run history (`/history`) and worker progress logs: steps that repeat work, stall, or wait on the user without need.
</Scope>

<Scope mode="messages">
Run `~/.claude/scripts/lib/py ~/.claude/scripts/nightly_review/nightly_review.py messages`: it reads the last 24 hours of live verdicts from `~/.local/state/notify/review.jsonl` and dry-reviews up to 20 replies that ask nothing, using `reply,all` guide rules. Discovery includes showrunners and natedev in each session's own account; recorded transcript paths take precedence, renamed showrunners retain their timer identity, and reply timestamps select the interval. Allow up to 20 minutes at the default review limit; do not impose a shorter command timeout.
Report live and sampled totals separately, failures by rule, `Unreviewed` separately, and questions absent from registration records separately. `check`, `replay`, and `sample` records are never live messages. Read tonight's messages and `~/rust/nate_style/message/`; propose at most one new rule with its stem, test, source quoting the user's words, and a Bad/Good pair from tonight. Write `DIR/messages.md` through the proposal and ledger process below; propose no rule when the evidence supports none. Never post a push or hold a reply.
</Scope>

<Proposal>
For each mode, pick the single change with the largest measurable gain for its cost, and measure its baseline tonight. Write `DIR/<mode>.md`:

```
# <title>
**Target:** config | rust/<target> | messages (~/rust/nate_style/message/<stem>.md)
**Innovation:** <the change, one or two sentences>
**Measurable improvement:** <metric>: <baseline tonight> → <target>, measured by <command or method>
**Gain per week:** <`N min` or `N h` of user-visible time (waiting, red CI, failed runs) and what it is, else `N% of tokens`, from tonight's numbers; `unmeasured` when neither can be measured>
**Evidence:** <files and lines, measurements>
**Cost:** <what implementing takes and risks>
**Also found:** <up to three one-line runners-up, each `- **<title>.** <one line> [gain/week: 12 min red CI]` or `[gain/week: 0.4% of tokens]`; leave the tag off an unmeasured one>
```

LEDGER gets one line for the proposal and one for each Also found item, with these gains, and `/next efficiency` and `/showrunner:nightly` rank by them. Write each gain as a week's worth in the form shown, so a script can read it. Anything that is no candidate (what was denied tonight, a note on the target choice) goes below the items under a plain line of its own, which closes the list.

Then `SendMessage` to the address `python3 ~/.claude/scripts/message/sessions.py address natedev` prints, which is natedev's session on this machine: first line `Nightly proposal ready: DIR/<mode>.md`, second line the title. If it prints nothing, natedev is not running: skip the message; `/next efficiency` reads every night's reports. After the last mode, stay open: the user may open its `nightly-review` tab in the `agents` herdr session to ask about it.
</Proposal>
