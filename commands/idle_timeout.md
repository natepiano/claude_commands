---
description: Set a machine's credential idle timeout end to end — edit, commit, then close the stale ssh masters and chase the other machine.
---

**Arguments**: $ARGUMENTS — a duration. Empty means the default, 60 minutes. Accepts a bare number (minutes), or a number with a unit: `1`, `30m`, `90 minutes`, `2h`, `37 hours`, `1 day`, `1d`.

Each host's entry file sets `nate.idleLockMinutes`: `/etc/nixos/configuration.nix` on natedev, `~/nixos/darwin.nix` on the Mac. The option is declared with **no default** in `modules/common/options.nix`, so those two files are the only place a value exists.

Four things derive from it on each platform: ssh `ControlPersist` (`modules/<platform>/ssh.nix`), the gh token cache TTL (`modules/<platform>/gh.nix`), the setup check against 1Password's auto-lock (`modules/<platform>/setup-check.nix`), and setup-check's own run interval (`modules/common/setup-check.nix`, so that one lands on both).

**Do not trust that list; check it.** Both machines carry an `idle timeout derived` line in `setup-check` that reads every derived value off the *running* system and names each one, what the machine has, and what the config wants. That is the authority; this prose has rotted twice. The trap: `14400` appears on both machines as the setup-check interval at four hours, so misattributing that number to a gh cache reads as plausible.

Both machines cache the gh token. The Mac's was removed in gen 19 and restored in `fcd32c3` after measurement refuted the premise — macOS approval is per shell, as on Linux, and the prompt names `iTermServer`; see `modules/darwin/gh.nix`. Never describe the Mac as having no gh cache. Every Claude Code Bash call is a fresh shell, so without the cache an agent pays one approval per `gh` invocation where a human pays one per shell and never notices.

Same semantics, different mechanisms, and only the mechanism matters when reading a value back. Linux: the kernel user keyring, TTL re-armed by `keyctl timeout` on every call. Mac: a login-keychain item `nixos-gh-token` plus a stamp file whose mtime is the idle clock, re-armed on use; the wrapper refuses a stale item on read, so the sweep only bounds how long an expired token rests. A keychain item carries no expiry of its own, so the check follows the wrapper's own store-path reference to its `expired` helper and reads the `-ge <n>` the cache expires by — one object, not a description of one. Never reintroduce an `idle-ttl-seconds=<n>` comment for the check to grep: a comment goes confidently wrong the day the behaviour changes.

1Password's auto-lock cannot be written from outside the app (its settings.json is HMAC-signed), so that step is always the user's, in the app.

<ExecutionSteps>
    **EXECUTE THESE STEPS IN ORDER:**

    **STEP 1:** Convert $ARGUMENTS to whole minutes. No unit or `m`/`min`/`minute(s)` means minutes; `h`/`hr`/`hour(s)` multiplies by 60; `d`/`day(s)` by 1440. Empty means 60. Refuse anything that is not a positive whole number of minutes (say why, stop).

    **STEP 2:** Ask the user (AskUserQuestion) whether this applies to **both machines** or **this one only**, offering "Both machines" first as recommended. Each host's setup check compares against *its own* `nate.idleLockMinutes`, and only the user knows which 1Password apps they have changed.

    **STEP 3:** Edit the entry file(s) for the chosen scope: `/etc/nixos/configuration.nix` and/or `darwin.nix`. Change only the `nate.idleLockMinutes = <n>;` line. If a nearby comment gives a rationale the new value falsifies — "kept tight because the screen never locks" at four hours — rewrite the comment to keep the fact and drop the false reason.

    **STEP 4:** Commit the changed file(s), named on the commit line, with `timeout: nate.idleLockMinutes <old> -> <new>` and the session attribution line the harness asks for.

    **STEP 5:** Tell the user, in one short message:
      1. Run `! rebuild` (it needs their sudo password, so you cannot run it). It applies the new values and pushes the commit.
      2. In 1Password on each machine in scope: Settings > Security > Auto-lock, set the same number of minutes. The menu offers fixed choices; if the exact value is missing, name the nearest above and below — a longer app lock costs only an extra prompt, a shorter one leaves cached credentials alive after the vault locks.
      3. Run `setup-check`; the `1Password auto-lock` line passes only when the two agree. Expect **one** authorization prompt from the `1Password CLI` check on Linux, which runs `op vault list` uncached on purpose.

    **STEP 6 — the ssh masters. Do not skip this, and do not merely mention it.**
    Open control masters keep the OLD `ControlPersist` until they expire or are closed. Wait until the user confirms the rebuild is done — closing them first just spawns a replacement carrying the old value.

    `ControlPath` is `~/.ssh/cm-%C`, a hash, so the socket name does not name the host:

        ls -la ~/.ssh/cm-* 2>/dev/null
        for h in git@github.com mac; do printf "%-16s " "$h"; ssh -O check "$h" 2>&1 | head -1; done

    A socket with no matching known host is still addressable: `ssh -O check -o ControlPath=<socket> dummy` reports it, and `-O exit` the same way closes it.

    If any master is running, **ask the user (AskUserQuestion) whether to close them now** — name the hosts, and that closing costs one new connection setup on next use while leaving them holds the old window for up to that long. On yes, close each with `ssh -O exit <host>` (or the explicit `ControlPath` form), re-check, and report each gone. If none are open, say so in one line.

    **STEP 7:** If STEP 2 chose both machines, message the other machine's session (SendMessage, after ListAgents for its name) to pull and rebuild, naming the commit and what derives from it on *that* platform. Log it with `inbox add <name> <text>` and commit `docs/inbox/natedev.md`. If that session is unreachable, tell the user the machine still needs a pull and rebuild by hand.
</ExecutionSteps>
