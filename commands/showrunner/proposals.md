---
description: List pending production proposals.
---

# Proposals

1. Run `"$HOME/.claude/scripts/lib/py" "$HOME/.claude/scripts/production/proposals_table.py"` in the showrunner session.
2. Print its output exactly.
3. Report an error in one line when the command fails.

The command finds the production through the session's update timer. It reads three sources:

1. Open issues tagged `proposal` in the production's `Proposals` folder.
2. Proposed or deferred entries in the production's `Improvement queue` ledger.
3. The units' add-ons that need a decision.

Question records share the issues folder, but they are not proposals. Use `/showrunner:questions` to list questions.

An issue belongs here when `proposed_by` names this production or one of its units. Each child production lists its own proposals.

Issue rows show the P number, title, proposer and creation date. Each style decision has one row per rule and crate.

Rule candidates remain outside this table. Use `style_proposals.py candidates` or `drop-candidate --idea "<text>"` to manage them.

The table columns are `Proposed by`, `Proposal`, `Date` and `State`. Waiting rows follow `/next` order and show any urgency level.

State identifies the next action: user review, work in progress, or deferral. An add-on identifies the showrunner as its decision owner.

Ask the owning unit to correct an unclear title. Do not rewrite it in the table.

With no open entries, the command prints `no open proposals`. It changes nothing.
