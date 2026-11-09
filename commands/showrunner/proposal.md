---
description: File and manage a production proposal.
---

# Proposal

A proposal is a hanadocs issue with the `proposal` tag.
The production document's `Proposals` field names its folder.
Use `/showrunner:proposals` to list open proposals.

Use this command for each field change:

```sh
"$HOME/.claude/scripts/lib/py" "$HOME/.claude/scripts/production/proposal_issue.py" <verb> <issue> …
```

The command locks the issue and sets `date_modified`.
Report command failures.
Use the proposal number and title when you name a proposal to the user.

| State | Fields | Command |
| --- | --- | --- |
| Waiting | `status: open`, `stage: backlog` | File or `unapprove` |
| In progress | `status: open`, `stage: active` | `approve` or `start` |
| Built | `status: closed`, `as_built` | `close --as-built <path>` |
| Declined | `status: closed`, `reason` | `/close_issue` |

## File

File style decisions through `style_proposals.py file`.
File other proposals through `/issue` with the tags `[issue, proposal]`.
Store the writer's fixed staff id in `proposed_by`.
Remove `showrunner` and `plan` from proposal frontmatter.
Keep plan details in the unit configuration.
Use `number <issue>…` to assign a P number.
The command reserves numbers across the issues folder and its adjacent archive.

Leave ranking fields empty until the user confirms the ranking.
Put the proposed ranking on one `Proposed ranking:` line in the body.
Put alternatives on a separate `Alternatives:` line.
Send the showrunner the issue path and proposed ranking.
Do not present new proposals to the user unless requested.
Use `urgency <issue> --level blocks|soon|normal|later` to set review priority.
Use `--due <ISO>` with `soon`.
Use `mark-important <issue>` when the user calls the proposal important.

## Approve

```sh
… proposal_issue.py approve <issue> --by user --unit <staff id>
```

Use `--by showrunner` for an approval without the user's answer.
The command stores the unit's fixed staff id in `unit`.
The command stores the calling showrunner's fixed staff id in `approved_in`.
A user approval replaces a showrunner approval.
A showrunner approval preserves a user approval.
Readers show current staff names.

Use `unapprove <issue> [--dry-run]` from the approving showrunner to remove a showrunner approval.
Only the user's decline removes a user approval.
Use `decline-answer <issue> --words '<user words>'` for a style decision's declined answer.
Use `style_proposals.py complete` after each merge to close completed style decisions.
Use `reopen <issue>` to reopen a closed decision with its recorded answer.
