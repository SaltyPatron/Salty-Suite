# Salty-Suite

Developer and IT tools, each its own product: one implementation, its own version and release, configured by its
callers rather than by names baked into it. Laplace uses them the way any other user does: a pinned release.

| Product | What it does | Version |
|---|---|---|
| [host-lock](host-lock/) | Cross-platform cooperative resource locking (`shared` / `exclusive`), writer preference, deadlines, stale-PID detection, and JSON receipts (Python 3 stdlib + POSIX `flock` sh) | [`host-lock/VERSION`](host-lock/VERSION) |
| [machine-env](machine-env/) | Declarative host/service config loader, variable expander, schema validator, and `shell`/`json`/`systemd`/`iis`/`msbuild` formatter (Python 3 stdlib) | [`machine-env/VERSION`](machine-env/VERSION) |
| [pg-tune](pg-tune/) | Hardware-topology-aware PostgreSQL 18 configuration calculator and drift checker (`shared_buffers`, WAL, `io_method`, worker/connection budgets; Python 3 stdlib) | [`pg-tune/VERSION`](pg-tune/VERSION) |
| [deps-installer](deps-installer/) | Pinned TSV dependency manifest planner, topological resolver, SHA-256 / commit verifier, and isolated prefix installer (Python 3 stdlib) | [`deps-installer/VERSION`](deps-installer/VERSION) |
| [artifact-store](artifact-store/) | Content-addressed blob store, immutable release materializer, verifier, GC pruner, and deterministic build fingerprinter (Python 3 stdlib) | [`artifact-store/VERSION`](artifact-store/VERSION) |

A release of a product is a tag `<product>-v<version>`; its tarball is attached to the GitHub Release.
