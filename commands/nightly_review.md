---
description: Nightly read-only review ending in one improvement proposal with a measured baseline. `config` covers machine configuration; `rust` covers ~/rust code, style and the development process. The nightly-review timer launches both on natedev at 01:00.
argument-hint: config | rust
---

Mode: `$ARGUMENTS`. NIGHT is today's local date, DIR is `~/.local/state/nightly-review/NIGHT`, LEDGER is `~/.local/state/nightly-review/ledger.md`.

<Rules>
- Read-only: write nothing outside DIR. No edits, commits, pushes, rebuilds or installs. A worktree only when a measurement needs a build that must not touch a working copy; remove it after.
- Start research subagents when a candidate needs more evidence than you can gather directly.
- Nobody is watching. A denied or failed action is a finding, not a blocker: note it and continue.
- Read LEDGER first. A proposal marked declined comes back only with new evidence, named as such.
</Rules>

<Scope mode="config">
Every memory (`~/.claude/projects/*/memory/`; the Mac's over `ssh mac` if it answers, else say it was skipped), `~/.claude` commands, skills, docs, scripts and settings, and `/etc/nixos` for both platforms. Look for duplication, rules that contradict each other, stale memories, configuration that could be simpler, and friction that recurs in `/etc/nixos/docs/inbox/natedev.md` and `docs/status.md`.
</Scope>

<Scope mode="rust">
One target a night. Take `hana` unless LEDGER's last `rust/` entry was hana; otherwise the entry after the last one named in: cargo-liner, bevy_brp, nateroids, obsidian_knife, showrunner.
- A project: its code against its conventions and `~/rust/nate_style`, and style rules that cost more than they catch.
- `showrunner`: the showrunner, producer and unit commands under `~/.claude/commands/` with their docs and scripts, against run history (`/history`) and delegate progress logs: steps that repeat work, stall, or wait on the user without need.
</Scope>

<Proposal>
Pick the single change with the largest measurable gain for its cost, and measure its baseline tonight. Write `DIR/<mode>.md`:

```
# <title>
**Target:** config | rust/<target>
**Innovation:** <the change, one or two sentences>
**Measurable improvement:** <metric>: <baseline tonight> → <target>, measured by <command or method>
**Evidence:** <files and lines, measurements>
**Cost:** <what implementing takes and risks>
**Also found:** <up to three one-line runners-up>
```

Then `SendMessage` natedev, first line `Nightly proposal ready: DIR/<mode>.md`, second line the title; if natedev is not listed, skip it, the morning hook finds the file. Stay open: the user may attach to ask about it.
</Proposal>
