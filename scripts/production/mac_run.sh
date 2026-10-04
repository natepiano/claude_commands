#!/usr/bin/env zsh
# Run a sha on the Mac: ship it from this machine, build hana, run it, check it
# stays up for SECONDS, then stop it. User, 2026-10-04: after each green CI on
# the merge branch, when the Mac is reachable.
#
# Usage: mac_run.sh <repo dir> <sha> [seconds, default 60]
# Exit: 0 stayed up; 3 Mac unreachable; 5 wrong tree; 6 build failed; 7 exited early.
set -o pipefail
repo=$1 sha=$2 hold=${3:-60}
host=natemccoy@mac
# A standing clone, so builds are incremental. Never ~/rust/hana, the user's.
clone=rust/hana_catalyst_mac
port=15710
log=/tmp/mac_run_hana.log

ssh -o ConnectTimeout=6 -o BatchMode=yes $host true 2>/dev/null || { print "mac unreachable"; exit 3 }
sha=$(git -C $repo rev-parse --verify "$sha^{commit}") || exit 2

# The Mac cannot fetch from GitHub over ssh, so the commits go as a bundle.
ssh $host "test -d $clone/.git || git clone -q ~/rust/hana $clone" || exit 4
base=$(ssh $host "git -C $clone rev-parse HEAD")
ref=refs/mac-run/tip
bundle=$(mktemp --suffix=.bundle)
git -C $repo update-ref $ref $sha
if git -C $repo cat-file -e "$base^{commit}" 2>/dev/null && [[ $base != $sha ]]; then
  git -C $repo bundle create -q $bundle $ref "^$base"
else
  git -C $repo bundle create -q $bundle $ref
fi
bundle_exit=$?
git -C $repo update-ref -d $ref
(( bundle_exit == 0 )) || { rm -f $bundle; exit 4 }
scp -q $bundle $host:/tmp/mac_run.bundle || { rm -f $bundle; exit 4 }
rm -f $bundle

ssh $host "git -C $clone fetch -q /tmp/mac_run.bundle $ref && git -C $clone checkout -q --detach FETCH_HEAD; rm -f /tmp/mac_run.bundle"
head=$(ssh $host "git -C $clone rev-parse HEAD")
print "HEAD=$head"
[[ $head == $sha ]] || { print "wrong tree: wanted $sha"; exit 5 }

ssh $host "cd $clone && ~/.cargo/bin/cargo build -q -p hana" || { print "build failed"; exit 6 }

# perl's alarm stops hana after the hold; 142 (SIGALRM) means it stayed up.
ssh $host "cd $clone && BRP_EXTRAS_PORT=$port perl -e 'alarm shift; exec @ARGV' $hold ./target/debug/hana > $log 2>&1"
run_exit=$?
if (( run_exit == 142 )); then
  print "stayed up ${hold}s"
  exit 0
fi
print "exited early: $run_exit"
ssh $host "tail -20 $log"
exit 7
