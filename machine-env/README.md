# machine-env

A generic declarative environment and host configuration loader, variable expander, JSON schema validator, and multi-format emitter using Python 3 standard library only.

## Features

- **Declarative parsing.** Reads `KEY=VALUE` and `export KEY=VALUE` lines (`UTF-8` / `UTF-8-BOM`), preserving backslashes in Windows paths without shell execution.
- **Strict safety checks.** Rejects duplicate keys (case-insensitive), NUL bytes, malformed assignments, and undefined `$VAR`, `${VAR}`, or `%VAR%` references.
- **Process precedence (`--process-precedence on|off`).** When `on` (default), non-empty process environment variables take precedence over file values and feed variable expansion; when `off`, file declarations win.
- **Optional schema validation (`--schema <path>`).** Validates required keys, `string`, `integer`, `bool`, `path`, and `enum` types, optional `additionalProperties: false`, and applies `default` values.
- **Output formats (`--format`):**
  - `shell`: POSIX `export KEY=VALUE`
  - `json`: JSON object
  - `systemd`: `Environment="KEY=VALUE"` lines with systemd escaping
  - `iis`: `<environmentVariables><add name="..." value="..." /></environmentVariables>` XML
  - `msbuild`: `<Project><PropertyGroup><KEY>VALUE</KEY></PropertyGroup></Project>` XML

## Usage

```sh
./machine-env --file /etc/myapp/machine.env --strict --schema schema.json --format systemd
./machine-env --file /etc/myapp/machine.env --source-key CONFIG_SOURCE --format shell
```

```powershell
.\machine-env.ps1 --file C:\ProgramData\MyApp\machine.env --process-precedence off --format msbuild
```

## Tests

```sh
py -3 -m unittest discover -s tests
```
