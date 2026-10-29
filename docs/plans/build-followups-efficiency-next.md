# efficiency — next items

## nix_env trusts copies.py copies  · status: approved (showrunner, 2026-10-29), waiting

**Source:** verify's copies.py survey (plan `docs/plans/verify-copies.md` on origin/verify-copies, 28354d2b); showrunner go as next, 2026-10-29.

**Why:** verify-copies moves every build copy, style_sync lint included, to `clone --shared` copies with their own git under `/tmp/claude/copies/<showrunner>-<unit>/<phase>-<purpose>`. `scripts/lib/nix_env.py` approves only `.envrc` files below the trusted roots or at the top of a registered linked worktree, so it refuses those copies ("direnv: .envrc is blocked").

**Rule (strict):** trust a copy's `.envrc` only when its copies.py record names a source repo under a trusted root, and the copy's `.envrc` equals the source's `.envrc` blob at the copy's commit. Anything else stays blocked.

**Start when both hold:** (1) the user approves verify-copies (done 2026-10-29, plan header); (2) verify sends its Phase 3 record format (path and fields). The plan's Cleanup section names the provenance fields (source path, source commit, copy commit, target mode, seed source) in a `scratch.py` record; the exact path and format are not fixed yet.

**Due:** before verify's Phase 4 moves the first caller.
