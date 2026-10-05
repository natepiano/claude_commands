#!/usr/bin/env zsh
# Run a sha on the Mac: ship it from this machine, build hana, run it, check it
# stays up for SECONDS, then stop it. User, 2026-10-04: after each green CI on
# the merge branch, when the Mac is reachable.
#
# Then it builds and runs each demo example for EXAMPLE_HOLD seconds with the
# same cargo command you would type, so `cargo run` in the clone starts at
# once. User, 2026-10-05: the Ian Hubert demo runs from this clone.
#
# Usage: mac_run.sh <repo dir> <sha> [seconds, default 60]
# Exit: 0 all stayed up; 3 Mac unreachable; 5 wrong tree; 6 build failed;
# 7 hana exited early; 8 hana stayed up but an example failed.
set -o pipefail
repo=$1 sha=$2 hold=${3:-60}
host=natemccoy@mac
# A standing clone, so builds are incremental. Never ~/rust/hana, the user's.
clone=rust/hana_catalyst_mac
port=15710
log=/tmp/mac_run_hana.log
example_hold=20
# crate, example, then its required feature when it has one.
examples=(
  "hana_diegetic typography"
  "hana_diegetic units"
  "hana_diegetic widgets"
  "hana_diegetic diegetic_text_stress"
  "hana_conduit playground"
  "hana_lagrange showcase fit_overlay"
  "hana_liminal all_modes"
  "hana_valence staggered_unfold"
  "hana_mimesis_tools show_beam"
)

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
# Run outside cargo, so Bevy needs the asset root that `cargo run` would give it.
ssh $host "cd $clone && BEVY_ASSET_ROOT=\$HOME/$clone/crates/hana BRP_EXTRAS_PORT=$port perl -e 'alarm shift; exec @ARGV' $hold ./target/debug/hana > $log 2>&1"
run_exit=$?
if (( run_exit == 142 )); then
  print "hana stayed up ${hold}s"
  result=0
else
  print "hana exited early: $run_exit"
  ssh $host "grep -v 'CommandQueue has un-applied' $log | tail -20"
  result=7
fi

failed=0
for entry in $examples; do
  parts=(${=entry})
  args="-p $parts[1] --example $parts[2]"
  (( $#parts == 3 )) && args="$args --features $parts[3]"
  example_log=/tmp/mac_run_$parts[2].log
  if ! ssh $host "cd $clone && ~/.cargo/bin/cargo build -q $args" > /dev/null 2>&1; then
    print "example $parts[2]: build failed"
    failed=1
    continue
  fi
  # cargo run execs the example, so the alarm reaches it; the pkill catches a
  # survivor. The bracket keeps pkill from matching its own shell.
  ssh $host "cd $clone && BRP_EXTRAS_PORT=$port perl -e 'alarm shift; exec @ARGV' $example_hold ~/.cargo/bin/cargo run -q $args > $example_log 2>&1"
  example_exit=$?
  ssh $host "pkill -f '[h]ana_catalyst_mac/target/debug/examples/$parts[2]'"
  if (( example_exit == 142 )); then
    print "example $parts[2]: stayed up ${example_hold}s  (cargo run $args)"
  else
    print "example $parts[2]: exited early: $example_exit"
    ssh $host "tail -10 $example_log"
    failed=1
  fi
done

(( result == 0 && failed )) && result=8
exit $result
