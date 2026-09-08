---
description: Set THIS machine's credential idle timeout (ssh masters, gh token cache, 1Password auto-lock) in its one place, then say what to do by hand.
---

**Arguments**: $ARGUMENTS — a duration. Empty means the default, 60 minutes. Accepts a bare number (minutes), or a number with a unit: `1`, `30m`, `90 minutes`, `2h`, `37 hours`, `1 day`, `1d`.

The timeout is per machine. Each host's entry file sets `nate.idleLockMinutes`: `/etc/nixos/configuration.nix` on natedev, `~/nixos/darwin.nix` on the Mac (the option itself is declared, with no default, in `modules/common/options.nix`). Three things on the machine read it: ssh `ControlPersist` (`modules/<platform>/ssh.nix`), the gh token cache (`modules/<platform>/gh.nix`), and the setup check that compares it with 1Password's own auto-lock. 1Password's value cannot be written from outside the app (its settings.json is HMAC-signed), so the last step is always the user's, in the app. The other machine is untouched.

<ExecutionSteps>
    **EXECUTE THESE STEPS IN ORDER:**
    **STEP 1:** Convert $ARGUMENTS to whole minutes. No unit or `m`/`min`/`minute(s)` means minutes; `h`/`hr`/`hour(s)` multiplies by 60; `d`/`day(s)` by 1440. Empty means 60. Refuse anything that is not a positive whole number of minutes (say why, stop).
    **STEP 2:** Pick this machine's entry file: `/etc/nixos/configuration.nix` on Linux, `~/nixos/darwin.nix` on macOS. Change its `nate.idleLockMinutes = <n>;` line to the new value. Touch nothing else.
    **STEP 3:** Commit that one file with the message `timeout: <hostname> nate.idleLockMinutes <old> -> <new>` and the session attribution line the harness asks for.
    **STEP 4:** Tell the user, in this order, in one short message:
      1. Run `! rebuild` (it needs their sudo password, so you cannot run it). The rebuild applies the new ssh `ControlPersist` and gh cache timeout and pushes the commit.
      2. In 1Password on this machine: Settings > Security > Auto-lock, set the same number of minutes. The app's menu offers fixed choices; if the exact value is not offered, say so and name the nearest choice above and below — a longer app lock only costs an extra prompt, a shorter one leaves cached credentials alive after the vault locks.
      3. Run `setup-check`; the `1Password auto-lock` line passes only when the two agree.
    **STEP 5:** Note that already-open ssh masters keep the old timeout until they expire or are closed; `ssh -O exit git@github.com` (and `ssh -O exit mac` on natedev) closes them now if the user wants the new value to apply immediately. The gh cache picks up the new timeout on its next use.
</ExecutionSteps>
