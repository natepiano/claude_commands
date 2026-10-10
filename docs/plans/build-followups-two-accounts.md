# two-accounts

> **Production: build-followups** — unit `two-accounts-unit`; production doc `docs/plans/build-followups-production.md`

## Source

2026-10-10 13:23 PDT

Investigate and propose a plan only; change nothing live (the user, 2026-10-10 13:2x PDT): "task either an existing unit dir or a new unit dir to investigate how we can do this. my goal would be to run showrunners in one account and unit directors in another - as there are more unit dirs than show runners it might allow showrunners to burn their weekly allowance slower and we can swap out unit directors maybe a little more frequently - or maybe they both can make it all the way through the week if we do it this way - just keeping two accounts continually acive / i don't want you to do it yet - i want you to investigate it - including maybe we have a second worktree for the second account so that we can keep them up to date / i think the ~.claude would be main always but then we could have ~/.claude1, ~/.claude2, ~/.claude3 as worktrees corresponding to each account that i actually have - and they can just merge changes to main as we make them - / but have someone go figure this out and propose a plan" Mechanism found: CLAUDE_CONFIG_DIR per account (https://code.claude.com/docs/en/authentication.md, Log in with multiple accounts); each dir has its own login, settings and history. Account switching today: scripts/whoami/.
