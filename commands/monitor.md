---
description: Switch which computer a monitor displays, over DDC/CI.
---

**Arguments**: $ARGUMENTS — an optional monitor (`dell`, `samsung`) and an optional destination (`mac`, `linux`), in either order. No destination means report the current state.

<ExecutionSteps>
    **EXECUTE THESE STEPS IN ORDER:**
    **STEP 1:** Run `bash ~/.claude/scripts/monitor/monitor.sh $ARGUMENTS`. It picks the backend for whichever machine you are on — ddcutil on Linux, m1ddc on the Mac — so pass the arguments through unchanged.
    **STEP 2:** Report the script output. Add no commentary when it succeeds.
    **STEP 3:** On a non-zero exit, report its stderr verbatim. Do not retry with different arguments and do not reach for `ddcutil`/`m1ddc` directly — the script already tries the Mac over ssh when the Dell cannot be driven from Linux, so a failure means both paths are gone.
</ExecutionSteps>

## Scope

`/monitor mac`, `/monitor linux`, `/monitor dell mac`, `/monitor dell linux` all switch the Dell S3425DW. Naming no monitor means every switchable one, today the Dell alone.

`/monitor samsung <anything>` exits non-zero with the reason. The Samsung C34J79x implements no DDC/CI: its EDID reads from both machines but i2c 0x37 never answers — over DisplayPort from Linux and Thunderbolt from the Mac, unrelated stacks, while it was displaying the Mac — and its OSD has no DDC/CI toggle. Use its buttons or its input auto-detection.

## When this misbehaves

- Keyboard and mouse do not follow the picture. Deskflow shares the Mac's over the network, so they reach the Linux box on screen or not.
- Either machine drives the Dell whichever one it is displaying. Both directions are tested, so a switch is never one-way.
- The Mac writing its own input code (`set input 27`) has only ever run through a stub. Reading from the Mac is proven and so is the `0x1b` value from the Linux side, so it is expected to work; the first real `/monitor dell mac` issued on the Mac settles it.
- The Linux script's ssh-to-the-Mac fallback should never fire. If it does it announces itself, and it needs the 1Password agent to approve the key, so it can sit waiting on a tap.
