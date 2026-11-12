# GPU size trial

The implementation provides local preparation, a test wrapper, comparison, and a NixOS system module.
The implementation also provides the AWS service and deployment changes.
The director runs real preparation after review.

## Purpose

Compare one Hana archive on natedev and the configured GPU types.
Resolve `~/rust/hana` at `refs/remotes/origin/main` once, or supply an explicit commit.
Run each test twice on each machine with zero retries.
Use hardware Vulkan on every machine.
Reject CPU adapters.

The wrapper runs each test as a child.
It reads `resource.getrusage(RUSAGE_CHILDREN).ru_maxrss` after the child exits.
The report names this measurement the largest process's peak RSS.
It excludes GPU memory.
The [nextest target runner](https://nexte.st/docs/features/target-runners/) supplies the binary and test arguments.
The wrapper preserves the child's exit status.

Different statuses on one machine mean `flaky`.
A flaky natedev test makes each comparison `inconclusive`.
Stable different statuses mean `difference`.
Stable equal statuses mean `same`.
Missing measurements mean `incomplete`.
Retain both wall times, statuses, and memory values.
Compare G4dn separately from the G6 size results.

## Preparation

The local command waits for build admission and a natedev hold release.
One supervised program builds the archive and closures, then runs the two natedev measurements.
The trial folder retains the source, Cargo dependencies, manifest, archive, closure export, wrapper, and measurements.
The manifest records the commit, tree, lock hashes, tool versions, configuration, and artifact hashes.
An interrupted preparation remains incomplete.

Use the Hana default shell for hardware Vulkan.
The CI shell can select a software driver.
Preserve the CI test selection, groups, and timeouts.
Build portable x86-64 binaries with this command:

```sh
nix develop .#default -c cargo nextest archive --profile ci --locked --workspace --all-features --tests --archive-file hana-tests.tar.zst
```

Build one GPU system closure from the deployment host's locked nixpkgs input.
The system module lives beside the existing NixOS patch: `docs/plans/builds-aws/gpu-trial.nix`.
It includes the production NVIDIA driver, Vulkan, the Hana runtime closure, and the Rust toolchain.
The Rust tools and nextest come from Nix store paths.
Old preparations require a new `prepare` run before the service can use them.
Use the Hana toolchain output when it exists.
Otherwise, use the pinned host nixpkgs and Rust overlay.
A Hana `rust-toolchain.toml` selects the Rust version when that file exists.
Fetch dependencies into a private Cargo cache.
Never copy the user's Rust or Cargo directories.
Check the toolchain wrappers for home paths.
Test Cargo with an empty `HOME`.
The L4 and T4 boxes use the official NixOS image that gates already use.
Each box imports the closure through signed URLs, runs `switch-to-configuration boot`, and reboots once.
The [Nix manual](https://nix.dev/manual/nix/2.34/command-ref/nix-store/import.html) describes the closure import format.
A systemd unit starts the installed trial runner after the reboot.
Record the boot, import, reboot, and runner start times.

## Online rules

Select the first configured region with an applied G and VT Spot quota of at least 32.
Refuse the run when no configured region qualifies.
Skip types with no offering in that region.
Never substitute another type or region.

| Type | Spot maximum USD/hour |
|---|---:|
| g6.xlarge | 0.81 |
| g6.2xlarge | 0.98 |
| g6.4xlarge | 1.32 |
| g6.8xlarge | 2.01 |
| g4dn.xlarge | 0.53 |

The configuration map supplies these types, their order, and their maximum prices.
Report `price above max` when a type's Spot price exceeds its maximum.
Do not query an on-demand price service.

Reuse `reserve_gate` and `resolve_gate` with mode `gpu-size-trial` and the trial ID.
Reserve the type maximum multiplied by the lifetime hours, plus the configured box extra.
Admit a box only when booked costs, open reservations, and the next reservation fit within USD6.
The existing daily and monthly limits still apply.
The box extra is USD0.05 for disk, IPv4, and transfer.

Run one box at a time.
Retain the trial ID, budget, region, and type order across restarts.
A saved budget stop ends the trial.
Use the original trial folder for a restart.
The estimated GPU root size is `gpu_size_trial_root_gib=100`.
The estimate includes the driver, runtime, archive extraction, and two test runs.
Count boot, import, reboot, tests, and delivery within the 60-minute lifetime.
Use early termination and expiry tags.
An uncertain termination blocks the next launch.
Reuse cleanup through `ExecStopPost`.
Do not install a signal handler.

## Commands

The session uses these local commands:

```sh
python3 -m scripts.aws_target.gpu_size_trial prepare --commit SHA
python3 -m scripts.aws_target.gpu_size_trial compare --trial-folder DIR
```

`prepare` also accepts `--repo` and `--trial-folder` for local fixtures and explicit destinations.
The folder must not exist.

The user runs these commands:

```sh
python3 scripts/aws_target/deploy.py up --workload merge-gates --approve-resources
python3 scripts/aws_target/deploy.py gpu-size-trial run --trial-folder DIR --approve-resources
```

The first command adds the configured GPU types to the scaler policy.
The second command starts the transient service.

## Phases

| Phase | Work | Validation |
|---|---|---|
| 1: offline | Configuration, prepare, NixOS module, wrapper, compare | Fake preparation, wrapper measurements, comparison rules, hardware check, holds, whole AWS suite, basedpyright |
| 2: online | Region, offerings, budget, upload, launch, results, cleanup, scaler policy, run command, operator documentation | Fake AWS lifecycle tests, budget and cleanup tests, whole AWS suite, basedpyright |

Real preparation and the paid trial follow review.
Phase 1 performs no AWS operations.
