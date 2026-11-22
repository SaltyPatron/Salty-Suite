# host-lock

A cooperative lock on one machine's shared resource, taken **shared** (readers: ingests, verifications) or
**exclusive** (writers: deployments, installs), with:

- **Writer preference.** Every taker first passes a turnstile (`PATH.turnstile`), held only while waiting to acquire; a
  waiting writer holds it, so later readers queue behind the writer while active readers drain.
- **Nesting (POSIX sh).** In `host-lock.sh`, a nested command inherits the lock descriptor and re-takes it at once.
- **Deadlines.** Nonnegative seconds (`0` refuses immediately) or `wait` to wait indefinitely; exit code `3` on timeout,
  `2` on misuse, `1` on I/O error.
- **Diagnostics & stale-PID recovery.** While waiting, reports active holders; `host_lock.py` maintains a sidecar
  metadata file (`PATH.holders.json`), recovers stale dead-PID records automatically, and optionally emits a JSON
  receipt (`--receipt`).

## Cross-platform Python CLI (`host_lock.py`)

Uses Python 3 standard library only (`msvcrt` byte-range + turnstile on Windows, `fcntl.flock` + turnstile on POSIX).
Lock files are never unlinked. Launchers: `host-lock.cmd`, `host-lock.ps1`.

```sh
python3 host_lock.py --mode exclusive --path /var/lock/myapp/resource.lock --wait 600 \
  --receipt /tmp/lock-receipt.json -- my-deploy --target staging
```

```powershell
.\host-lock.ps1 --mode shared --path C:\ProgramData\MyApp\resource.lock --wait 30 '--' my-verify
```

## POSIX shell library & command (`host-lock.sh`, `host-lock`)

POSIX `sh` (dash, bash) and `flock` (util-linux):

```sh
. host-lock.sh
host_lock exclusive 8 /run/lock/myapp/resource.lock 600 || exit   # 0 held, 3 deadline passed, 2 misused, 1 cannot open
# ... descriptor 8 holds the lock until it is closed or the shell exits

host-lock shared /run/lock/myapp/resource.lock wait -- my-ingest --source x
```

## Tests

- Cross-platform Python tests: `py -3 -m unittest discover -s tests` (or `python3 -m unittest discover -s tests`)
- POSIX shell tests: `HL_SHELL=dash bash tests/host-lock.sh` and `HL_SHELL=bash bash tests/host-lock.sh`
