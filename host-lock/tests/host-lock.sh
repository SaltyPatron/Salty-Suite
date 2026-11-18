#!/usr/bin/env bash
# shellcheck disable=SC2016,SC1090  # each candidate expands its own variables; HL is a path set above
# host-lock.sh: shared and exclusive holders, nesting, deadlines, a queued waiter, writer preference, misuse, and the
# host-lock command. Each candidate closes descriptor 8 so it stands for an independent process, not a nested command.
set -euo pipefail
here=$(cd "$(dirname "$0")/.." && pwd)
work=$(mktemp -d "${TMPDIR:-/tmp}/host-lock.XXXXXX"); trap 'rm -rf "$work"' EXIT
export LOCK=$work/resource.lock HL=$here/host-lock.sh W=$work
S=${HL_SHELL:-bash}   # the shell every candidate process sources host-lock.sh in: run the tests under dash and bash
. "$HL"
take() { $S -c '. "$HL"; host_lock "$1" 8 "$LOCK" "${2:-0}"' _ "$@"; }

host_lock shared 8 "$LOCK" 0
take shared 8>&-
rc=0; take exclusive 8>&- 2>/dev/null || rc=$?; [[ $rc == 3 ]]
echo 'PASS a reader admits another reader and refuses a writer'
flock -u 8

host_lock exclusive 8 "$LOCK" 0
for mode in shared exclusive; do rc=0; take $mode 8>&- 2>/dev/null || rc=$?; [[ $rc == 3 ]]; done
echo 'PASS a writer refuses readers and writers'
take exclusive
echo 'PASS a nested command takes its holder'"'"'s lock again at once'
rc=0; take exclusive 8>&- 2>/dev/null || rc=$?; [[ $rc == 3 ]]
flock -u 8
take exclusive 8>&-
[[ -f "$LOCK" ]]
echo 'PASS release admits the next; the lock file stays'

host_lock exclusive 8 "$LOCK" 0
$S -c '. "$HL"; host_lock exclusive 8 "$LOCK" 5 && touch "$W/acquired"' 8>&- 2>"$work/waiting" &
waiter=$!
for _ in {1..100}; do grep -q 'Waiting up to' "$work/waiting" && break; sleep .02; done
grep -q 'Waiting up to' "$work/waiting"; [[ ! -e "$work/acquired" ]]
flock -u 8; wait "$waiter"; [[ -e "$work/acquired" ]]
echo 'PASS a waiter waits for release, then holds'

host_lock exclusive 8 "$LOCK" 0
$S -c '. "$HL"; host_lock exclusive 8 "$LOCK" wait && touch "$W/queued"' 8>&- 2>"$work/queue" &
queued=$!
for _ in {1..100}; do grep -q 'Waiting for' "$work/queue" && break; sleep .02; done
grep -q 'Waiting for' "$work/queue"; kill -0 "$queued"; [[ ! -e "$work/queued" ]]
flock -u 8; wait "$queued"; [[ -e "$work/queued" ]]
echo 'PASS wait waits as long as it takes'

host_lock exclusive 8 "$LOCK" 0
rc=0; $S -c '. "$HL"; host_lock shared 8 "$LOCK" 1 && touch "$W/wrong"' 8>&- 2>/dev/null || rc=$?
[[ $rc == 3 && ! -e "$work/wrong" ]]
echo 'PASS a deadline that passes holds nothing'
for bad in -1 x 1.5; do rc=0; take exclusive "$bad" 8>&- 2>/dev/null || rc=$?; [[ $rc == 2 ]]; done
rc=0; $S -c '. "$HL"; host_lock both 8 "$LOCK" 0' 8>&- 2>/dev/null || rc=$?; [[ $rc == 2 ]]
rc=0; $S -c '. "$HL"; host_lock shared 7 "$LOCK" 0' 8>&- 2>/dev/null || rc=$?; [[ $rc == 2 ]]
flock -u 8
echo 'PASS misuse is refused'

# Writer preference: a writer waiting behind a reader holds the turnstile, so a later reader waits behind the writer.
host_lock shared 8 "$LOCK" 0
$S -c '. "$HL"; host_lock exclusive 8 "$LOCK" 10 && { touch "$W/written"; sleep 1; }' 8>&- 2>"$work/writer" &
writer=$!
for _ in {1..100}; do grep -q 'Waiting up to' "$work/writer" && break; sleep .02; done
$S -c '. "$HL"; host_lock shared 8 "$LOCK" 10 && { [ -e "$W/written" ] && touch "$W/after"; }' 8>&- 2>/dev/null &
reader=$!
sleep .3; [[ ! -e "$work/written" ]]
flock -u 8; wait "$writer"; wait "$reader"
[[ -e "$work/after" ]]
echo 'PASS a later reader waits behind a waiting writer'

# The command: holds while COMMAND runs, its status out; refused when held.
"$here/host-lock" exclusive "$LOCK" 0 -- sh -c 'exit 7' || rc=$?; [[ $rc == 7 ]]
host_lock exclusive 8 "$LOCK" 0
rc=0; "$here/host-lock" shared "$LOCK" 0 -- touch "$work/ran" 8>&- 2>/dev/null || rc=$?
[[ $rc == 3 && ! -e "$work/ran" ]]
flock -u 8
"$here/host-lock" shared "$LOCK" -- touch "$work/ran"; [[ -e "$work/ran" ]]
echo 'PASS host-lock runs a command holding the lock'
echo 'host-lock: ok'
