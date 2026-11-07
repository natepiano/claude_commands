# AWS build target: one command, from nothing to a working target


## Goal

The user said on 2026-11-03: "we need to be able to set a box up to run soup to nuts - idempotently - we could destroy this instance and start from scratch and it would work - i don't want to be involved - i want it to work".

1. One user command, `deploy.py up --approve-resources`, takes the target from no resources to a working, routed AWS build target.
2. A second run of `up` changes nothing. It checks each stage and reports it as present.
3. `up` continues after a failure or an interruption. It continues from the first stage that is not complete.
4. `deploy.py down` removes the target. After `down`, `up` builds the target again with no manual step.
5. No step edits a tracked file. No step prints a secret.
6. The user runs only `up` and `down`. Sessions make no AWS call. The `--approve-resources` flag stays the user's consent to spend.

## One-time user inputs

The user creates these private files outside the repository.

1. Run `deploy.py secrets` in a terminal to create `~/.config/aws-build/admin.gpg`, mode `0600`.
2. Store the AWS administrator key and the Tailscale OAuth client in that encrypted file.
3. Give the OAuth client three scopes: `auth_keys` for `tag:build-aws`, `devices:core` for `tag:build-aws` (`down` deletes the device), and `policy_file`.
4. Keep `~/.config/aws-build/credentials` for the unattended `build-scaler` profile only.
5. Keep the existing private Budget address file outside the repository.

`secrets` imports an existing administrator profile and removes it after encryption succeeds.
It also imports and removes the old `tailscale-oauth` file.
The user can retain stored values with Enter and replace one value.
The helper asks for one passphrase when a deployment first needs administrator credentials.
It keeps decrypted values only in memory for that process.
Symmetric AES256 encryption disables the GPG symmetric passphrase cache.
`down --approve-destroy` uses the same terminal credential boundary.
Without that flag, `down` prints a plan and changes nothing.
Removal preserves the spend history and encrypted credentials.
The OAuth client also needs device deletion access for `tag:build-aws`.

`up` needs only this repository and the reviewed `/etc/nixos` checkout.
The proof crate includes its own locked development shell and has no Rust dependencies.

## Stages of `up`

Each stage checks the live state first. It acts only when the state is not complete.

1. **Price.** Read the current Spot price into the price state file (`deploy.py price`, landed in 92f439dc0).
2. **Bootstrap.** Render the bootstrap and the remote settings.
3. **Stacks.** Prepare, then create or reconcile the foundation stack and the compute stack.
   - A stack in `REVIEW_IN_PROGRESS` (a failed first change set, no resources) counts as absent.
   - The preparation age is checked at the start of each stage, not across a CloudFormation wait.
4. **Scaler key.** Check the `build-scaler` profile with `sts get-caller-identity`. When the check fails, create a new key, write it to the credentials file, check it, then delete the old keys. The secret never reaches the screen.
5. **Tailscale policy.** A tracked policy source holds the `tag:build-aws` rules: natedev's user and the Mac's user reach the box and enter `natepiano` through Tailscale SSH; the box starts no connection. `up` applies it through the Tailscale API when the live policy differs. It changes only rules for `tag:build-aws`, and it prints each change it makes (showrunner ruling, 2026-11-03).
6. **Box join.** Wait for the instance in SSM. Ask the Tailscale API for a single-use, pre-approved, short-lived auth key for `tag:build-aws`. Send `tailscale up --ssh --advertise-tags=tag:build-aws` to the box through SSM with that key. No key is stored on the box, in git or in CloudFormation.
7. **Box configuration.** From natedev, switch the box to the `aws-build` output of natedev's `/etc/nixos` main with `nixos-rebuild --target-host`. The nixos repository is private, so the box does not fetch it. The box configuration lives only in `/etc/nixos`. A change to it goes to natedev as a branch, and `up` only reads that repository (showrunner ruling, 2026-11-03).
8. **Checks.** Confirm SSM, the `/home` mount on the expected data volume, the remote lifecycle service, and plain `rustc`, `cargo nextest` and `cargo mend` versions on both hosts.
9. **Idle stop.** Confirm the box stops itself within `idle_stop_minutes` plus a margin, through EC2.
10. **Activation.** Turn on routing to AWS from state outside the repository, then start `aws-target.service`. The registry rows land in the tracked config by a reviewed commit, with the user's approval of each value.
11. **Routing proof.** Write the crate from `scripts/aws_target/routing_proof.py` into a scratch Git repository.
   The folder uses task `gate:aws-routing-proof`.
   The next `up` removes a folder left by a dead run. Run it through the normal pinned queue. Confirm `target=aws` in its record.

Configuration, image creation, and the idle stop proof close AWS routing before they change the box.
Each stage waits without a time limit for AWS jobs that still run.
Kept claims do not delay the wait.
A stage with a valid proof leaves routing unchanged.
Activation opens routing after the idle stop proof.
An interrupted `up` leaves routing closed until the next `up` reaches activation.
Jobs use natedev and the Mac while AWS routing stays closed.

## Phases

1. **Stack lifecycle.** Stages 1 to 4, `down`, the `up` stage runner and its state record. Fixes: `REVIEW_IN_PROGRESS`, the preparation age, and one settings reader for `price`.
2. **Box join and configuration.** Stages 5 to 8.
3. **Activation and proof.** Implementation complete; the user deployment proof remains. Stages 9 to 11. Then the user runs `down` and `up` once, to prove a rebuild from nothing.
4. **Gate trial evidence.** In progress. A cloud gate trial passes clean at each size. The report gives each size's time in four parts: request to running, instance setup, tests, and teardown. It also gives CPU use during the tests. The test time is the time to expect from a prepared image. User, 2026-11-07.
5. **Prepared trial image.** After phase 4. A saved image holds the tools, so a box starts without the setup step. The phase decides when to rebuild the image after natedev changes. The trial then measures setup again, to show the time saved. User, 2026-11-07.

## Removal status

`down` is implemented.
It disables routing before removing the device, keys, compute, foundation, retained volume, and Budget.
It removes setup state and preserves spend history.
The user deployment proof remains.
