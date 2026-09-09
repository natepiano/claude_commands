---
description: Set a machine's credential idle timeout end to end — edit, commit, then close the stale ssh masters and chase the other machine.
---

**Arguments**: $ARGUMENTS — a duration. Empty means the default, 60 minutes. Accepts a bare number (minutes), or a number with a unit: `1`, `30m`, `90 minutes`, `2h`, `37 hours`, `1 day`, `1d`.

Each host's entry file sets `nate.idleLockMinutes`: `/etc/nixos/configuration.nix` on natedev, `~/nixos/darwin.nix` on the Mac. The option is declared with **no default** in `modules/common/options.nix`, so the entry files are the only place a value exists — do not go looking for a `default =` line there.

What derives from it differs by platform, and getting this wrong misreports the effect:

* **natedev (Linux)** — four things: ssh `ControlPersist` (`modules/linux/ssh.nix`), the gh token cache TTL in the kernel user keyring (`modules/linux/gh.nix`, `idleLockMinutes * 60` seconds), the setup check comparing it with 1Password's auto-lock (`modules/linux/setup-check.nix`), and **setup-check's own run interval** (`modules/common/setup-check.nix`, `intervalSeconds = idleLockMinutes * 60`).
* **Mac** — four things: ssh `ControlPersist` (`modules/darwin/ssh.nix`), the gh token cache TTL (`modules/darwin/gh.nix`), the same auto-lock check (`modules/darwin/setup-check.nix`), and the same setup-check run interval — that one is in `modules/common`, so it lands on **both** platforms.

**Do not trust this table; check it.** Both machines carry an `idle timeout derived` line in `setup-check` that reads every derived value back off the *running* system and names each one, what the machine has, and what the config wants. That is the authority — this prose is a summary of it and can rot, which it twice has. The specific trap: `14400` appears on both machines as the setup-check interval at four hours, so a wrong attribution of that number to a gh cache reads as plausible and survives a casual check.

  The Mac's cache has a history worth knowing, because a stale reading of it caused a wrong report on 2026-09-08. Gen 19 **removed** it, on the premise that the macOS app holds a CLI approval across process trees until it locks, which would make 1Password's own auto-lock the gh expiry. That premise was **refuted by measurement** (recorded in `modules/darwin/gh.nix`): macOS approval is **per shell**, the same as Linux — three `op` calls in one shell cost one approval and the later two returned in ~1 s with no prompt, while a call from a new shell prompted again, every time, with the app demonstrably unlocked throughout. The prompt names `iTermServer`, one long-lived process shared by every shell, so the grant is scoped tighter than the process it names. The cache was **restored in `fcd32c3`** on that evidence. Do not describe the Mac as having no gh cache.

  The consequence, which matters whenever an agent is involved: a human in a terminal pays one approval per shell and never notices, but **every Claude Code Bash call is a fresh shell**, so an agent would pay one per `gh` invocation without a cache.

  The two caches have the same *semantics* and different *mechanisms*, and only the mechanism differs when reading a value back. Linux: the kernel user keyring, TTL re-armed by `keyctl timeout` on every call, readable from the wrapper on the profile. Mac: a login-keychain item `nixos-gh-token` plus a stamp file whose mtime is the idle clock, re-armed on use, with a constant-interval sweep that only bounds how long an expired token rests — the gate does not depend on the sweep, because the wrapper refuses a stale item on read. A keychain item carries no expiry of its own, so the Mac's TTL is read by following the wrapper's own reference: the wrapper names its `expired` helper by store path because it *runs* it, and the check follows that path and reads the `-ge <n>` that the cache actually expires by. As on Linux, the value the check reads is the value the code uses — one object, not a description of one. Every break in that chain (no wrapper, no expired path inside it, no `-ge` line) extracts empty, mismatches, and reports STALE. An earlier version of this used an `idle-ttl-seconds=<n>` comment in the wrapper; `modules/darwin/gh.nix` now tells the next person not to reintroduce that, and this file agrees — a check that greps a comment goes confidently wrong on the day someone changes the behaviour and leaves the comment.

1Password's auto-lock cannot be written from outside the app (its settings.json is HMAC-signed), so that step is always the user's, in the app.

<ExecutionSteps>
    **EXECUTE THESE STEPS IN ORDER:**

    **STEP 1:** Convert $ARGUMENTS to whole minutes. No unit or `m`/`min`/`minute(s)` means minutes; `h`/`hr`/`hour(s)` multiplies by 60; `d`/`day(s)` by 1440. Empty means 60. Refuse anything that is not a positive whole number of minutes (say why, stop).

    **STEP 2:** Ask the user (AskUserQuestion) whether this applies to **both machines** or **this one only**. Offer "Both machines" first as the recommended answer. The reason to ask rather than assume: each host's setup check compares against *its own* `nate.idleLockMinutes`, so a host whose config moves while its 1Password app does not — or the reverse — reports a mismatch. Only the user knows which apps they have changed.

    **STEP 3:** Edit the entry file(s) for the chosen scope: `/etc/nixos/configuration.nix` and/or `darwin.nix`. Change only the `nate.idleLockMinutes = <n>;` line. If a nearby comment gives a *rationale* that the new value falsifies — e.g. "kept tight because the screen never locks" at four hours — rewrite the comment to keep the fact and drop the false reason. Do not leave a comment that now lies.

    **STEP 4:** Commit the changed file(s), named on the commit line, with `timeout: nate.idleLockMinutes <old> -> <new>` and the session attribution line the harness asks for.

    **STEP 5:** Tell the user, in one short message:
      1. Run `! rebuild` (it needs their sudo password, so you cannot run it). It applies the new values and pushes the commit.
      2. In 1Password on each machine in scope: Settings > Security > Auto-lock, set the same number of minutes. The menu offers fixed choices; if the exact value is not offered, name the nearest above and below — a longer app lock costs only an extra prompt, a shorter one leaves cached credentials alive after the vault locks.
      3. Run `setup-check`; the `1Password auto-lock` line passes only when the two agree. Expect **one** 1Password authorization prompt from the `1Password CLI` check on Linux, which runs `op vault list` uncached on purpose — that is not a bug.

    **STEP 6 — the ssh masters. Do not skip this, and do not merely mention it.**
    Already-open control masters keep the OLD `ControlPersist` until they expire or are closed, so the new value does not fully apply until they are gone. Wait until the user confirms the rebuild is done — closing them first just spawns a replacement carrying the old value.

    Then find them. `ControlPath` is `~/.ssh/cm-%C`, a hash, so the socket name does not name the host:

        ls -la ~/.ssh/cm-* 2>/dev/null
        for h in git@github.com mac; do printf "%-16s " "$h"; ssh -O check "$h" 2>&1 | head -1; done

    A socket with no matching known host can still be addressed directly:
    `ssh -O check -o ControlPath=<socket> dummy` reports it, and `-O exit` the same way closes it.

    If any master is running, **ask the user (AskUserQuestion) whether to close them now** — say which hosts, and that closing costs nothing beyond one new connection setup on next use, while leaving them means the old window persists for up to that long. If they say yes, close each with `ssh -O exit <host>` (or the explicit `ControlPath` form), then re-check and report that each is gone. If none are open, say so in one line and move on.

    **STEP 7:** If STEP 2 chose both machines, message the other machine's session (SendMessage, after ListAgents to get its name) telling it to pull and rebuild, naming the commit and what derives from it on *that* platform per the table above. Log the message with `inbox add <name> <text>` and commit `docs/inbox/natedev.md`. If no session for the other machine is reachable, tell the user that machine still needs a pull and rebuild by hand.
</ExecutionSteps>
