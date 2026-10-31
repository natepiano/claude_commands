# efficiency — next items

## nix_env trusts standing gate checkouts · status: superseded by Phase 18

Phase 18 uses `copies.py run --owner <unit-or-production> --repo <source> --commit HEAD -- <command>`.
Standing checkouts live below `~/worktrees/gates`, already a trusted direnv root.
The former proposal for approving disposable copies is no longer needed.
