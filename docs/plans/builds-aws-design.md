# AWS builds: current design

Status: current implementation, checked on 2026-11-12.
AWS runs temporary Spot boxes for merge gates, paired gate trials, and the GPU size trial.
Normal build jobs use `mac` and `natedev`.
AWS has no permanent builder or build offload route.

## Per-run merge gates

Each gate reserves queue capacity and spending before it requests a box.
The gate uses its own box and terminates that box after completion or failure.
The configured regions are `us-east-2`, `us-west-2`, `eu-north-1`, and `eu-south-2`.
The home region, `us-east-2`, holds the shared artifact bucket and gate role.
Regional stacks supply launch templates, public subnets, and expiry functions.
The saved `gate-deployment.json` identifies deployed resources.
The configured region list alone does not prove deployment or available quota.

The gate reads current Spot prices before its first launch ranking.
It places variants under a capacity cooldown last.
It prefers larger vCPU counts within each group.
Within one size, it prefers the lowest expected gate cost across regions and zones.
Expected cost includes compute, regional transfer, and any configured regional delay cost.
Missing live prices retain applicable cached prices.
Capacity refusals select the next variant.
A normal gate uses natedev after all variants refuse.
The saved ranking and client tokens keep retries consistent.

Boxes boot an official NixOS image and obtain the tools and source needed for that run.
Signed URLs transfer inputs, logs, and results.
Boxes receive no scaler credential file.
The launch record, log, and queue progress identify the selected region and zone.

## Paired gate trials

A paired trial compares base and changed commits on the same box for each selected test module.
It reserves all required cloud slots and costs before launch.
Each box runs both commits once before measurement.
It then alternates their order across measured rounds.
Temporary Git branches make the commits available to the boxes.
Trial cleanup removes those branches and terminates the boxes.
The report includes sample results, median CPU savings, cost estimates, and cleanup status.

## GPU size trial

The trial compares one prepared Hana test archive on natedev and temporary GPU boxes.
Local preparation builds the archive, a Nix toolchain, and a NixOS system with the NVIDIA Vulkan driver.
It uses a private Cargo cache and checks Cargo with an empty `HOME`.
Each machine runs the archive twice with zero retries.
The report compares statuses, wall times, and the largest single process's peak RSS.
It excludes GPU memory.

The service selects the first candidate region with an applied G and VT Spot quota of at least 32 vCPUs.
It checks offerings and Spot prices for each configured type.
It runs one box at a time and confirms termination before the next launch.
Each box imports the prepared system and reboots before testing.
A restart retains the trial ID, region, type order, and budget.
The complete trial has a separate USD6 reservation limit.
The normal daily and monthly caps also apply.

## Spending and cleanup

`config/aws_target.conf` supplies account limits and billing settings.
`config/cloud_gate.conf` supplies per-run settings.
The ledger combines account bills, completed costs, and open reservations before it admits new spending.
Historical builder costs remain in the reports.
Builder removal stops new builder storage charges in the resource inventory.
The configuration files remain the source for cap values.

Each service requests termination in its cleanup path.
`ExecStopPost` repeats cleanup after the worker stops.
The ledger preserves uncertain launches until reconciliation confirms their state.
Guest shutdown and regional expiry functions supply separate lifetime bounds.
AWS failures can delay termination beyond those bounds.

## Removed builder: 2026-11-04

`builder-removal.json` records the removal on 2026-11-04.
The persistent `aws-build` instance, Tailscale device, and data volumes were removed.
`routing.json` records `enabled: false`.
The target registry contains `mac,natedev`.
The builder configuration and offload route have been removed.
Builder slots, warm mirrors, idle stop, and queue-based starts are historical features.
Only historical cost, ownership, and teardown compatibility remain.

See the [runbook](builds-aws-runbook.md) for current commands.
See the [as-built document](../as-built/builds-aws-target.md) for implementation details.
