# artifact-store

Content-addressed local artifact store (`blobs/sha256/<prefix>/<hash>`), immutable release materializer (`releases/<id>`), verifier, garbage collector, and deterministic build fingerprinter using Python 3 standard library only.

## Features

### `artifact-store` (`artifact_store.py`)
- **Content-addressed blobs**: stores payloads under `blobs/sha256/<prefix>/<hash>` with staging + integrity re-check and atomic replacement.
- **Immutable releases**: materializes source trees into `releases/<id>` via hardlinks (with `shutil.copy2` fallback across filesystems) and an atomic `.artifact-receipt.json` receipt.
- **Link & ACL safety**: rejects symlink/junction traversal in ancestors, sources, releases, targets, and blobs; applies Windows ACL inheritance (`icacls.exe /reset /Q`) to staged artifacts.
- **Advisory locking**: coordinates concurrent store access with `msvcrt.locking` on Windows and `fcntl.flock` on POSIX.
- **Verification & GC**: `verify` validates release receipts, payload hashes, and blob integrity; `gc` prunes unreferenced releases and unreferenced blobs while preserving releases referenced by `current`, `refs/`, `--keep <id>`, or `--keep-latest <N>`.

### `build-fingerprint` (`build_fingerprint.py`)
- Computes a deterministic canonical SHA-256 fingerprint and JSON receipt over:
  - compiler identity and version (`--compiler`)
  - compiler/linker flags and floating-point contract settings (`--flags`)
  - CPU ISA / `cpuid` features and target architecture (`--isa` / `--no-host-cpuid`)
  - math library declarations or static archive hashes (`--libm`)
  - deterministic sorted file/directory content trees (`--input ...`)

## Usage

```sh
artifact-store materialize --store /var/lib/artifacts --source ./dist --ref current
artifact-store verify --store /var/lib/artifacts
artifact-store gc --store /var/lib/artifacts --keep <id>

build-fingerprint --compiler gcc --flags "-O2 -ffp-contract=off" --input ./src --format json
build-fingerprint --compiler icx --flags "-fp-model=precise -ffp-contract=off" --input ./src --format sha256
```

## Tests

```sh
python3 -m unittest discover -s tests -v
```
