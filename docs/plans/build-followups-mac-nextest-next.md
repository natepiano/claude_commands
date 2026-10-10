# Mac test offload — Next

## Items to consider

- [x] **`status` says when an expired block is stuck**
  - Target: `scripts/mac_test/mac_test.py` `command_status`
  - Why needed: a block past its time limit stays in place when `expire_block` cannot turn CI's Mac switch back on (a cold gpg-agent is the usual cause). `status` then prints a lift time in the past beside "CI's Mac switch: off (this block)", with no word that the lift failed; the hourly "Mac block could not lift" message is the only notice. See `docs/as-built/mac-test-offload.md`.
  - Completion condition: while that state lasts, `status` prints one line naming the failed lift and the remedy (run `github-warmup`, then `unblock`), with a test.
  - Revealed by: the phase where a block stops CI's Mac job and expires. The user approved it on 2026-10-10.
