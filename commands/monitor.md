---
description: Switch which computer a monitor displays, over DDC/CI.
---

**Arguments**: $ARGUMENTS — an optional monitor (`dell`, `samsung`) and an optional destination (`mac`, `linux`), in either order. No destination means report the current state.

<ExecutionSteps>
    **EXECUTE THESE STEPS IN ORDER:**
    **STEP 1:** Run `bash ~/.claude/scripts/monitor/monitor.sh $ARGUMENTS`. It picks the backend for whichever machine you are on — ddcutil on Linux, m1ddc on the Mac — so pass the arguments through unchanged rather than translating them.
    **STEP 2:** Report the script output to the user. Do not add commentary when it succeeds; the script says what happened.
    **STEP 3:** If it exits non-zero, report its stderr verbatim. Do not retry with different arguments and do not reach for `ddcutil`/`m1ddc` directly — the script already tries the Mac over ssh when the Dell cannot be driven from Linux, so a failure means both paths are gone.
</ExecutionSteps>

## What it can and cannot do

`/monitor mac`, `/monitor linux`, `/monitor dell mac`, `/monitor dell linux` all switch the Dell S3425DW. Naming no monitor means every switchable one, which today is the Dell alone.

`/monitor samsung <anything>` prints why it cannot work and exits non-zero. The Samsung C34J79x implements no DDC/CI: its EDID reads perfectly from both machines, but i2c address 0x37 never answers — over DisplayPort from Linux, and over Thunderbolt from the Mac, which are unrelated stacks. It was displaying the Mac during that test, so it is not a case of a panel answering only on its live input, and its OSD has no DDC/CI toggle. Use its buttons or its own input auto-detection.

## Notes that matter when this misbehaves

Keyboard and mouse do not follow the picture. There is no KVM in either monitor; deskflow already shares the Mac's keyboard and mouse over the network, so they reach the Linux box whether or not it is on screen.

Either machine can drive the Dell no matter which one it is displaying. Both directions are tested, so a switch is never one-way and you can always get the screen back from where you are sitting.

The Linux script keeps an ssh-to-the-Mac fallback anyway, for the case where the direct path stops working — the failure it covers is the one that would otherwise strand you. It should never fire; if it does, it announces itself, and it needs the 1Password agent to approve the key, so it can sit waiting on a tap.
