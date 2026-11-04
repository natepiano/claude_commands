# AWS build target: one command, from nothing to a working target

> **Production:** builds (unit aws-unit). Branch `builds-aws`.

## Goal

The user said on 2026-11-03: "we need to be able to set a box up to run soup to nuts - idempotently - we could destroy this instance and start from scratch and it would work - i don't want to be involved - i want it to work".

1. One user command, `deploy.py up --approve-resources`, takes the target from no resources to a working, routed AWS build target.
2. A second run of `up` changes nothing. It checks each stage and reports it as present.
3. `up` continues after a failure or an interruption. It continues from the first stage that is not complete.
4. `deploy.py down` removes the target. After `down`, `up` builds the target again with no manual step.
5. No step edits a tracked file. No step prints a secret.
6. The user runs only `up` and `down`. Sessions make no AWS call. The `--approve-resources` flag stays the user's consent to spend.

## One-time user inputs

These inputs are private files outside the repository, mode 0600. The user makes each one once.

1. `~/.config/aws-build/credentials`, profile `build-admin`. It exists.
2. `~/.config/aws-build/notification-email`. It exists.
3. `~/.config/aws-build/tailscale-oauth`: a Tailscale OAuth client with the scopes `auth_keys` (tag `tag:build-aws`) and `policy_file`. It is new. `up` refuses with one clear line when the file is missing.

## Stages of `up`

Each stage checks the live state first. It acts only when the state is not complete.

1. **Price.** Read the current Spot price into the price state file (`deploy.py price`, landed in 92f439dc0).
2. **Bootstrap.** Render the bootstrap and the remote settings.
3. **Stacks.** Prepare, then create or reconcile the foundation stack and the compute stack.
   - A stack in `REVIEW_IN_PROGRESS` (a failed first change set, no resources) counts as absent.
   - The preparation age is checked at the start of each stage, not across a CloudFormation wait.
4. **Scaler key.** Check the `build-scaler` profile with `sts get-caller-identity`. When the check fails, create a new key, write it to the credentials file, check it, then delete the old keys. The secret never reaches the screen.
5. **Tailscale policy.** A tracked policy source holds the `tag:build-aws` rules: natedev's user and the Mac's user reach the box and enter `natepiano` through Tailscale SSH; the box starts no connection. `up` applies it through the Tailscale API when the live policy differs.
6. **Box join.** Wait for the instance in SSM. Ask the Tailscale API for a single-use, pre-approved, short-lived auth key for `tag:build-aws`. Send `tailscale up --ssh --advertise-tags=tag:build-aws` to the box through SSM with that key. No key is stored on the box, in git or in CloudFormation.
7. **Box configuration.** From natedev, switch the box to the `aws-build` output of natedev's `/etc/nixos` main with `nixos-rebuild --target-host`. The nixos repository is private, so the box does not fetch it.
8. **Checks.** Confirm SSM, the `/home` mount on the expected data volume, the remote lifecycle service, and `rustc`, `cargo nextest` and `cargo mend` inside the configured shell.
9. **Idle stop.** Confirm the box stops itself within `idle_stop_minutes` plus a margin, through EC2.
10. **Activation.** Turn on routing to AWS from state outside the repository, then start `aws-target.service`. The registry rows land in the tracked config by a reviewed commit, with the user's approval of each value.
11. **Routing proof.** Run one small test through the normal queue. Confirm `target=aws` in its record.

## Phases

1. **Stack lifecycle.** Stages 1 to 4, `down`, the `up` stage runner and its state record. Fixes: `REVIEW_IN_PROGRESS`, the preparation age, and one settings reader for `price`.
2. **Box join and configuration.** Stages 5 to 8.
3. **Activation and proof.** Stages 9 to 11. Then the user runs `down` and `up` once, to prove a rebuild from nothing.
