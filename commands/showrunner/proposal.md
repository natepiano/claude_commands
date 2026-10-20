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

It edits that one file, sets `date_modified`, and prints what changed.

Each proposal carries a number, `proposal_id: "P12"`, as a change row carries
`C12`. `number <issue>…` gives the next free number to each proposal without
one, oldest first, and skips other issues; `create` numbers a new style
decision. Numbers are never used again: the highest in the issues folder and
in the vault's `archive/` beside it is taken. Name a proposal to the user by
its number and title, such as `P12 selection widgets toggle`. On a
missing file or one without frontmatter it exits 2: report the line.

| State | `status` / `stage` | Set by |
| --- | --- | --- |
| waiting for approval | open / backlog | filing (`proposed_by`) |
| in progress | open / active | `approve` (`showrunner`, `unit`, `plan`), or `start` for a plan issue that has no approval |
| built | closed | `close` (`as_built` or `reason`) |

The footer's proposal count and the table's `waiting for your review` rows are
the waiting proposals only; approval moves a proposal to in progress.

## File

A style decision is filed by `style_proposals.py file`, never `/issue`.
It covers one rule and crate under one rule text. The user answers once;
later findings under that text keep the answer. Use `style_proposals.py sites`
for all findings and `candidates` for off-rule ideas. A candidate is never a
proposal; drop one with `style_proposals.py drop-candidate --idea "<text>"`. Other finished proposals become issues through `/issue`.

For other proposals, tag `proposal` beside
`issue`, and `proposed_by` the unit or session that wrote it; `/issue` then
gives it its number. Drafts stay out.
The writer proposes all five ranking values (`backlog_goal`,
`backlog_alignment`, `backlog_impact`, `backlog_urgency`, `backlog_effort`) at
once. The values reach the frontmatter only after the user confirms them, since
the vault ranks every open issue by them. A unit has no user in its session, so
it leaves the five fields empty, puts its values on a `Proposed ranking:` line
at the top of the issue body, and sends the showrunner the issue path, one line,
and the five values.

Do not show a new proposal to the user unasked (user, 2026-10-09: proposals
accumulate for them to review). They ask for one at a time (`/next proposal`),
and after their answer the next waits until they ask. When they review one,
show its proposed values in one plain-text line; they confirm or change them in their reply. Then write
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

Proposals approved before 2026-10-20 move with `proposal_issue.py
move-approved <issues folder>` (`--dry-run` first); a second run moves none.

Refill builds its approved findings in small tickets. The decision stays open
until every finding is built or waived. Decline only through:

```sh
~/.claude/scripts/lib/py ~/.claude/scripts/style/style_proposals.py decline --repo <style checkout> --issue <issue> --words '<the user’s words>'
```

For other proposals, send the approved issue to the unit that builds it.
The unit adds phases that cite it and puts it on the plan's `> **Issues:**`
line. Record that plan and its phases with the same `approve` command.
`--showrunner` is the session name from `showrunners.py name <slug>`;
`--unit` is the full name in the production's Units table. Approve sets the
stage active: the proposal is in progress and leaves the waiting count. Other
declined proposals close through `/close_issue`.

## Start and close

Style refill marks a decision active. Run `style_proposals.py complete` after
each merge. It closes only approved decisions, when all findings are built or
waived. It also closes an approved decision with no merged ticket when no
finding is left to build. A decision closed with nothing left to build opens
again with its approval for a later finding, as a decision closed as built does.
A finding is built only when its change is in the code. Repair a decision
or an old issue that closed too early with `style_proposals.py reopen --repo
<style checkout> --issue <issue> --parent <slug>`; add `--dry-run` to see the
lines first. It exits 1 when a built rename has no check; read the code and
pass `--finding <id>` (not applied) or `--applied <id>`, with `--by <who>`.
Filing withdraws a filed finding that an `exception` under its rule's newer
text supersedes, and closes a decision left with no filed finding. Run that step
alone with `style_proposals.py withdraw --repo <style checkout> --proposals
<folder>`; add `--dry-run` to see the lines first.
Filing first moves a finding to the one file that now declares its item, marks a
filed rename built when its new name is declared, leaves a filed finding whose
item is gone from its file, and withdraws the rest; run that step alone with `style_proposals.py
follow --repo <style checkout> --proposals <folder>`, and add `--dry-run` to see
the lines first.
Drop the bare duplicates of qualified renames from every open decision with
`style_proposals.py drop-duplicates --repo <style checkout>`; add `--dry-run`
to see the lines first. When a plain merge, not a ticket, built filed findings,
mark them built with `style_ledger.py held built --repo <style checkout> --id
<id> [--id <id> ...] --commit <merge>`; add `--by <who>` after reading the code
of a finding the check cannot prove.
For other proposals, `/unit:direct` marks plan issues active, and its final
as-built pass closes them with the as-built doc.
