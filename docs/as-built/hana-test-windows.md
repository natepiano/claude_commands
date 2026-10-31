# Hana test windows

Every agent Hana launch names the feature under test in `HANA_TEST_PURPOSE`
and its unit director in `HANA_TEST_OWNER`. The owner defaults to the caller's
`SHOWRUNNER_UNIT_ID`. `hana_shot.py launch` and captures with `--launch` take
`--purpose` and `--owner`. A launch without a non-empty purpose refuses before
starting a process. `mac_run.sh` and `smoke_launch.sh` take the same labels
from the caller's environment. The Mac command keeps both labels when it
starts from Terminal.

The BRP launch hook checks the Hana target's `env.HANA_TEST_PURPOSE` before
the hold and memory checks. An empty or missing purpose denies the launch
and names the key to add. Other targets use the existing memory gate.

`scripts/hanas/hanas.py list` reads natedev's process table and the Mac's
`ps eww` output over SSH. On macOS it reads the local process table with
`ps`, without `/proc`. Darwin environment reads use herdr's stable command
and environment boundary. It identifies executables at `target/<profile>/hana`
and prints machine, pid, BRP port, owner, purpose, age and checkout. Missing
labels print as `unlabelled`. An unreachable Mac prints one line and leaves
natedev's rows visible. Mac host and SSH options come from `mac_test.conf`.
An environment read failure keeps the process in the list with unknown labels
and reports the cause. Stop refuses that row, and sweep leaves it alone while
it handles other processes and returns a failure status.

`hanas stop <machine>:<pid|port>` stops a matching test Hana. An already-gone
process succeeds. `hanas sweep --dry-run` lists test Hanas over their limits;
`hanas sweep` sends TERM and then KILL after the configured grace period if
the process remains. Collection saves the process start identity. Each TERM
and KILL checks that identity again, so a reused PID receives no signal. A
stop prints success only after the saved process is gone. Sweep reports each
failed stop, continues with the other processes, and exits non-zero if any
stop failed. No owner-session record is
needed: the process carries its own labels.

`config/hanas.conf` holds the exempt ports, labelled lifetime, unlabelled
lifetime and stop grace. Each invocation reads those values through
`config_values`. A Hana is a test when it has either test label, uses a port
outside the exempt set, or carries `CLAUDECODE`, `SSH_CONNECTION` or
`SHOWRUNNER_UNIT_ID`. A Hana on an exempt port with none of these markers is
a user Hana, and neither stop nor sweep signals it. A test with neither label
uses the shorter unlabelled limit.

Tests inject process files, environment reads, SSH output and signals. They
never signal a real process or SSH to the Mac. Installation of the `hanas`
wrapper and the scheduled sweep belongs to the director after checkpoint.
