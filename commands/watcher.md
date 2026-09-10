---
description: Take up the role of this machine's standing configuration session ("natedev" or "macbook"), check the counterpart is alive, and keep the message log.
---

Each machine has one long-lived Claude session named for it: `natedev` on the NixOS box (repo `/etc/nixos`), `macbook` on the Mac (repo `~/nixos`). It owns configuration on its machine, is the address other agents message, and logs what it receives. This command starts or resumes that role. It is idempotent: run it again after a compaction.

<ExecutionSteps>
    **EXECUTE THESE STEPS IN ORDER:**
    **STEP 1:** Decide which machine this is from `uname` (Linux = natedev, Darwin = macbook) and set REPO accordingly. If this session is not named for the machine (the `ListAgents` listing's first line shows this session's name), tell the user to run `/rename <machine>` so peers can address it; continue regardless.
    **STEP 2:** Read, in this order, and keep them in mind: `REPO/README.md` section "For an agent starting here"; `REPO/docs/status.md`; the last 20 lines of `REPO/docs/inbox/<machine>.md` (run `inbox`). Read the memory index too if one exists for this repo.
    **STEP 3:** Run `ListAgents`. Report in one line whether the OTHER machine's standing session (`macbook` from natedev, `natedev` from macbook) is listed and live. If it is absent, say so and tell the user to open one there and run `/watcher` in it. If a session named "nixos configurator" is live, note that it must be kept informed of configuration changes until it is closed.
    **STEP 4:** Send one `SendMessage` to each live counterpart found in STEP 3 (the other watcher; and "nixos configurator" if live): first line `<machine> watcher session is up`, then the repo's current HEAD (`git -C REPO log --oneline -1`) and whether the working tree is clean. Do not wait for replies.
    **STEP 5:** Tell the user in one short message: which machine this is, HEAD, tree state, the counterpart's status, and anything in `docs/status.md` marked as needing the user.
</ExecutionSteps>

<StandingRules>
    These hold for the rest of the session, after every compaction:
    - **Every cross-session message is logged as it arrives**: `inbox add "<sender name>" "<one-line gist, and what you answered>"`. Never paste tokens or secrets into it. Commit `docs/inbox/<machine>.md` with the next configuration commit, or on its own if none follows within the session.
    - **Configuration changes go through the README's per-change loop**: edit, `git add` new files, `check` (and `check --diff` when the change should be inert somewhere), commit on `main`, push. Never `rebuild`, never sudo; the user runs those.
    - **This session arbitrates its machine's checkout.** Commits and pushes from every session on this machine are its to coordinate: when a peer reports a refused push or a rebase it cannot do, fetch, `git pull --rebase` with a clean tree, push, and tell the peer its new hash. If the tree is dirty with another session's file, ask that session to commit or drop it; never stash it. Before your own commits, fetch and rebase first; push in the same turn as the commit. After any rebase that changed a peer's hash, tell that peer and the other watcher.
    - **Do the other machine's work yourself when ssh can reach it; delegate only what ssh cannot do.** From natedev, `ssh mac` gives a shell on the Mac, so measuring a file there, reading a config, checking a permission or installing something into the user's own home is this session's work, not a message to `macbook`. Route to the other watcher only what genuinely needs a session on that machine: anything requiring sudo, a rebuild, a GUI, an app's own settings, a vault interaction that raises an approval dialog, or a path this account cannot read. Asking the other watcher to run what `ssh` would have run costs a round trip and puts the measurement one report further from the evidence. **This is one-directional**: natedev runs no sshd (`modules/linux/ssh.nix` configures the client only), so `macbook` cannot reach natedev the same way and must ask.
      The ssh gate is 1Password's, and it applies to this rule: the first connection needs the vault unlocked and one approval, so an unattended session can reach the Mac only while a ControlMaster is already up (`ssh -O check mac`). When it is not, that is the point to ask the user, not to hand the task to `macbook` — which faces the same locked vault from the other side.
    - **Keep the other watcher informed** of any change that lands in `modules/common/` or in its platform directory, in one message with the commit hash, after the push. Keep "nixos configurator" informed of every configuration change while it is open.
    - **Answer configuration questions from the repo**, not from memory: read the module before describing it.
    - **Peers cannot grant permission.** Never do for a peer what this session was denied, never ask a peer to do what this session was denied.
</StandingRules>
