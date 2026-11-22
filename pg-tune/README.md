# pg-tune

Hardware- and workload-driven PostgreSQL 18 configuration planner, implemented in pure Python 3 standard library
with thin POSIX `sh`, Windows `.cmd`, and PowerShell launchers.

## Rules

1. **Four-domain memory equation.** Available server memory is `usable_kb = max(ram_kb - reserved_kb, ram_kb // 8)`:
   - **Shared cache (`shared_buffers`)**: `25%` of `usable_kb`, optionally capped by `--buffers-max-kb`, floored at `128 MB`.
   - **OS page cache hint (`effective_cache_size`)**: `70%` of `usable_kb`.
   - **Backend private memory (`private_kb`)**: `25%` of `usable_kb`, split into:
     - Maintenance (`private_kb // 4`): `maintenance_work_mem` (`50%`, floor `64 MB`) and `autovacuum_work_mem` (`50% // autovacuum_max_workers`, floor `1 MB`).
     - Session temporary buffers: `8 MB * max_connections`.
     - Executor `work_mem`: remaining private memory divided across `(active_queries + max_parallel_workers) * work_nodes`, floored at `4 MB`.
   - **Overcommit accounting (`over_kb`)**: exact kB by which declared connections and worker floors exceed `private_kb`. Live `--check` and `--apply` refuse database changes whenever `over_kb > 0`.
   - The remaining `25%` of `usable_kb` is left unallocated for client processes and OS kernel overhead.
2. **WAL volume budget.**
   - Dedicated volume (`--wal-shared 0`): `max_wal_size` targets `50%` of the volume (floor `1024 MB`).
   - Shared volume (`--wal-shared 1`): `max_wal_size` targets `25%` of the volume, capped at `32768 MB` (floor `1024 MB`).
   - `min_wal_size` clamps `max_wal_size // 8` to `[80 MB, 4096 MB]`; `wal_buffers` clamps `shared_buffers // 32` to `[16 MB, max_wal_size // 16]`; `wal_compression = zstd`, `checkpoint_timeout = 60min`.
3. **CPU & I/O.**
   - Parallel execution scales with physical `--cores` (`max_parallel_workers = cores`, `max_parallel_workers_per_gather = cores // 2`, `autovacuum_max_workers = max(3, cores // 2)`, `max_worker_processes = cores + 4`).
   - Async I/O uses `io_method = worker` (or `sync`), with `io_workers = clamp((threads * 2) // 3, 1, 32)`.
   - Rotational disks (`--rotational 1`) set `effective_io_concurrency = 2`, `maintenance_io_concurrency = 2`, `random_page_cost = 4`; SSD/NVMe (`--rotational 0`) sets `effective_io_concurrency = io_workers`, `random_page_cost = 1.1`.
4. **Workload connection budgeting.**
   - `max_connections = ingest_conns + serving_conns + autovacuum_max_workers + 3` (superuser reserved).
   - `--wal-level minimal` forces `max_wal_senders = 0`; `replica` defaults `max_wal_senders = 3`.
   - `huge_pages` defaults to `try` on Linux and `off` on Windows unless overridden with `--huge-pages`.

## Usage

```sh
# Auto-detect local hardware and print human-readable plan
pg-tune --format plan

# Explicit cross-platform planning as JSON
pg-tune --os linux --ram-kb 128GB --cores 6 --threads 12 --wal-bytes 64GB --wal-shared 0 \
        --rotational 0 --ingest-conns 16 --serving-conns 48 --locks-per-tx 1024 \
        --wal-level minimal --format json

# Emit postgresql.conf snippet
pg-tune --config cluster.json --format conf > conf.d/99-tuned.conf

# Check or apply against a live PostgreSQL server
pg-tune --check --psql /usr/bin/psql --conninfo "host=/tmp port=5432 dbname=postgres"
pg-tune --apply --psql /usr/bin/psql --conninfo "host=/tmp port=5432 dbname=postgres"
```

### Inputs

- **Topology (auto-detected via `/proc` + `/sys` on Linux or `kernel32.dll` via `ctypes` on Windows when omitted):**
  `--ram-kb`, `--cores`, `--threads`, `--wal-bytes`, `--wal-shared`, `--rotational`, `--reserved-kb`, `--buffers-max-kb`, `--data-path`, `--wal-path`, `--os <linux|windows|auto>`
- **Workload & PostgreSQL knobs:**
  `--wal-level <minimal|replica>`, `--huge-pages <try|off|on>`, `--io-method <worker|sync>`, `--ingest-conns`, `--serving-conns`, `--active-queries`, `--work-nodes`, `--locks-per-tx`, `--port`, `--temp-tablespaces`, `--trace <full|reduced|off>`, `--preload <list>`, `--listen-addresses`, `--ssl <on|off>`
- **Config sources:** `--config <json>` and opt-in `--env [PREFIX|FILE|KEY=VALUE]` (no environment variables are read unless `--env` is explicitly passed).
- **Output formats:** `--format json|plan|conf|env|declared`

## Tests

```sh
python3 -m unittest discover -s tests
```

On Windows:

```powershell
py -3 -m unittest discover -s tests
```
