# host-lock

A cooperative lock on one machine's shared resource, taken **shared** (readers: ingests, verifications) or
**exclusive** (writers: deployments, installs), with:

- **Writer preference.** `flock` grants a new reader past a waiting writer, so a stream of readers can hold a writer
  off until its deadline. Every taker first passes a turnstile (`PATH.turnstile`), held only while it waits; a waiting
  writer holds it, so later readers queue behind the writer while the readers already in drain.
- **Nesting.** A command started by the holder inherits the descriptor and takes the lock again at once, never waiting
  behind a writer that waits for its own parent.
- **Deadlines.** Seconds, `0` to refuse at once, or `wait` to wait as long as it takes; nothing is held after a
  deadline passes.
- **Diagnostics.** While waiting, the processes that hold the lock (pid, start time, command line).

POSIX `sh` (dash, bash) and `flock` (util-linux).

```sh
. host-lock.sh
host_lock exclusive 8 /run/lock/myapp/resource.lock 600 || exit   # 0 held, 3 deadline passed, 2 misused, 1 cannot open
# ... descriptor 8 holds the lock until it is closed or the shell exits

host-lock shared /run/lock/myapp/resource.lock wait -- my-ingest --source x   # run a command holding the lock
```

Tests: `HL_SHELL=dash bash tests/host-lock.sh` and `HL_SHELL=bash bash tests/host-lock.sh`.
