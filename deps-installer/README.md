# deps-installer

A generic, dependency-free Python 3 standard library installer and verifier for pinned TSV dependency manifests.

## Manifest Schema

Tab-separated columns (lines starting with `#` and blank lines are ignored; `-` represents an empty value):

```text
name	version	kind	origin	ref	pin	build	profile	platforms	depends	use	proof
```

- **kind**: `source`, `release`, `tool`, `data`
- **pin**: 40- or 64-character hex commit SHA (`source`), or 64-character SHA-256 hex digest (`release`, `tool`, `data`)
- **platforms**: comma-separated target platforms (for example `linux,windows`)
- **depends**: comma-separated dependency names (topologically sorted with cycle detection)
- **proof**: path relative to the dependency's prefix proving installation (`.pin` for unpacked archives)

## Layout

- Sources & downloaded archives: `<src-dir>/<name>` (archives under `<src-dir>/<name>/<version>`)
- Out-of-tree builds: `<build-dir>/<name>`
- Isolated installs: `<prefix>/<name>/<version>` (override per package via `--prefix-override name=path`)
- Data artifacts: `<data-dir>/<proof>` (defaults to `<prefix>/data`)

## Toolchain Profiles

Built-in generic templates (customizable or extendable via `--profile-config <json>`):

- `icx-deterministic`: Intel LLVM (`icx`/`icpx`) with strict IEEE-754 floating-point semantics (`-fp-model=precise` / `/fp:precise`, `-ffp-contract=off`, `-fno-fast-math`, `-fsigned-zeros`, `-march=x86-64-v2`)
- `icx`: Intel LLVM with precise floating-point model
- `upstream-default`: standard upstream defaults without custom floating-point flags
- `none`: no compiler profile (source-only, header-only, prebuilt archives, data)

## Usage

```sh
deps-installer --manifest dependencies.tsv --platform linux --prefix /opt/deps --src-dir /opt/src --build-dir /opt/build --plan
deps-installer --manifest dependencies.tsv --platform linux --prefix /opt/deps --src-dir /opt/src --build-dir /opt/build --apply
deps-installer --manifest dependencies.tsv --prefix /opt/deps --status
deps-installer --manifest dependencies.tsv --prefix /opt/deps --verify-proofs
```

Windows:

```powershell
.\deps-installer.ps1 --manifest dependencies.tsv --platform windows --prefix C:\deps --plan
```

## Tests

```sh
python3 -m unittest discover -s tests -v
```
