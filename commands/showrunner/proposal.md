---
description: File a finished proposal as a hanadocs issue, record the user's approval, and see it started and closed.
---

# Proposal

A proposal is a hanadocs issue tagged `proposal`, in the folder the production
doc's `Proposals` line names. `/showrunner:proposals` lists the open ones. There
is no index to add a row to. Every frontmatter change after filing goes through:

```sh
"$HOME/.claude/scripts/lib/py" "$HOME/.claude/scripts/production/proposal_issue.py" <verb> <issue> …
```

It edits that one file, sets `date_modified`, and prints what changed. On a
missing file or one without frontmatter it exits 2: report the line.

| State | `status` / `stage` | Set |
| --- | --- | --- |
| written | open / backlog | `proposed_by` |
| approved, not started | open / backlog | `showrunner`, `unit`, `plan` |
| being built | open / active | |
| built | closed | `as_built` |

## File

A style decision is filed by `style_proposals.py file`, never `/issue`.
It covers one rule and crate under one rule text. The user answers once;
later findings under that text keep the answer. Use `style_proposals.py sites`
for all findings and `candidates` for off-rule ideas. A candidate is never a
proposal; drop one with `style_proposals.py drop-candidate --idea "<text>"`. Other finished proposals become issues through `/issue`.

For other proposals, tag `proposal` beside
`issue`, and `proposed_by` the unit or session that wrote it. Drafts stay out.
The writer proposes all five ranking values (`backlog_goal`,
`backlog_alignment`, `backlog_impact`, `backlog_urgency`, `backlog_effort`) at
once. The values reach the frontmatter only after the user confirms them, since
the vault ranks every open issue by them. A unit has no user in its session, so
it leaves the five fields empty, puts its values on a `Proposed ranking:` line
at the top of the issue body, and sends the showrunner the issue path, one line,
and the five values.

Do not show a new proposal to the user unasked (user, 2026-10-09: proposals
accumulate for them to review). When they review one, show its proposed values
in one plain-text line; they confirm or change them in their reply. Then write
all five and delete the `Proposed ranking:` line:

```sh
… proposal_issue.py stars <issue> --goal "1 - Ship Hana" --alignment 3 --impact 3 --urgency 1 --effort 2
```

Never ask star by star, and never through a form.

## Approve

For a style decision, record the user's approval with its unit's own plan
copy and no phase numbers:

```sh
… proposal_issue.py approve <issue> --showrunner "<name>" --unit <unit> --plan "<unit plan copy>"
```

Refill builds its approved findings in small tickets. The decision stays open
until every finding is built or waived. Decline only through:

```sh
~/.claude/scripts/lib/py ~/.claude/scripts/style/style_proposals.py decline --repo <style checkout> --issue <issue> --words '<the user’s words>'
```

For other proposals, send the approved issue to the unit that builds it.
The unit adds phases that cite it and puts it on the plan's `> **Issues:**`
line. Record that plan and its phases with the same `approve` command.
`--showrunner` is the session name from `showrunners.py name <slug>`;
`--unit` is the full name in the production's Units table. The stage stays
backlog until work starts. Other declined proposals close through `/close_issue`.

## Start and close

Style refill marks a decision active. Run `style_proposals.py complete` after
each merge; it closes the decision when all findings are built or waived.
For other proposals, `/unit:direct` marks plan issues active, and its final
as-built pass closes them with the as-built doc.
