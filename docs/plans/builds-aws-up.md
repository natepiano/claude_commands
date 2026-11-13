# AWS deployment: current commands

The permanent builder was removed on 2026-11-04.
The former twelve-stage builder deployment no longer exists.
Builder startup, bootstrap, idle shutdown, Tailscale enrollment, activation, and routing proof code have been removed.

`deploy.py up --approve-resources` now deploys temporary gate resources only.
The optional `--workload merge-gates` argument selects the same path.
This command requires the saved foundation ownership record.
It does not register an AWS SSH build target.

After complete removal, create the foundation at the user's terminal:

1. Run `deploy.py prepare` to validate inputs and record the foundation plan.
2. Run `deploy.py apply --approve-resources` to create the Budget and scaler foundation with compute disabled.
3. Run `deploy.py up --approve-resources` to deploy gate resources.

Foundation application confirms the private scaler credential file.
The retained deployment settings also support ownership checks, Budget maintenance, and teardown.
Historical costs remain in the ledger after removal.

Use `deploy.py prices` for regional Spot prices and quota evidence.
Use `deploy.py quota-request` for regional quota requests.
Use `aws_target.py tick` for account charges and cached gate prices.
Use `deploy.py down` to review the removal plan.
Use `deploy.py down --approve-destroy` for approved resource removal.

See the [runbook](builds-aws-runbook.md) for credentials, trials, cleanup, and current deployment steps.
