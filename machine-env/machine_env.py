#!/usr/bin/env python3
"""Generic declarative environment loader, expander, schema validator, and formatter."""
from __future__ import annotations

import argparse
from collections.abc import Mapping
import json
import os
from pathlib import Path
import re
import shlex
import sys
from xml.sax.saxutils import escape as xml_escape

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_MISUSE = 2

KEY_RE = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*$')
ASSIGN_RE = re.compile(r'^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$', re.S)
VAR_REF_RE = re.compile(r'\$\{([A-Za-z_][A-Za-z0-9_]*)\}|\$([A-Za-z_][A-Za-z0-9_]*)|%([A-Za-z_][A-Za-z0-9_]*)%')
INT_RE = re.compile(r'^[+-]?\d+$')
BOOL_VALUES = {'true', 'false', '1', '0', 'yes', 'no', 'on', 'off'}


class ConfigError(ValueError):
    """Raised when configuration loading, expansion, or validation fails."""


class _UsageParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        self.print_usage(sys.stderr)
        print(f'{self.prog}: error: {message}', file=sys.stderr)
        raise SystemExit(EXIT_MISUSE)


def parse_env_text(content: str) -> list[tuple[str, str]]:
    """Parse declarative KEY=VALUE lines with backslash path preservation and duplicate rejection."""
    if '\0' in content:
        raise ConfigError('NUL byte in configuration')

    lexer = shlex.shlex(content, posix=True)
    lexer.whitespace = '\r\n'
    lexer.whitespace_split = True
    lexer.escape = ''  # Backslashes in Windows paths are literal data.

    seen: set[str] = set()
    entries: list[tuple[str, str]] = []
    try:
        tokens = list(lexer)
    except ValueError as exc:
        raise ConfigError(f'Invalid configuration syntax: {exc}') from exc

    for item in tokens:
        if not item.strip():
            continue
        match = ASSIGN_RE.fullmatch(item)
        if not match:
            raise ConfigError(f'Invalid configuration assignment: {item.strip()}')
        name, value = match.groups()
        if '\0' in value:
            raise ConfigError(f'NUL byte in configuration key: {name}')
        upper = name.upper()
        if upper in seen:
            raise ConfigError(f'Duplicate configuration key: {name}')
        seen.add(upper)
        entries.append((name, value))

    return entries


def _lookup_mapping(mapping: Mapping[str, str], key: str) -> str | None:
    if key in mapping:
        return mapping[key]
    return None


def _expand_value(raw: str, scope: Mapping[str, str], fallback: Mapping[str, str]) -> str:
    def replace(match: re.Match[str]) -> str:
        ref = next(part for part in match.groups() if part is not None)
        if ref in scope:
            return scope[ref]
        ext = _lookup_mapping(fallback, ref)
        if ext is not None:
            return ext
        raise ConfigError(f'Undefined configuration reference: {ref}')

    return VAR_REF_RE.sub(replace, raw)


def _validate_value(name: str, value: str, spec: dict) -> None:
    if '\0' in value:
        raise ConfigError(f'NUL byte in configuration key: {name}')

    kind = spec.get('type', 'string')
    allowed = spec.get('values')
    if allowed is None:
        allowed = spec.get('enum')
    if allowed is None:
        allowed = spec.get('choices')

    if kind in ('integer', 'int'):
        stripped = value.strip()
        if not INT_RE.fullmatch(stripped):
            raise ConfigError(f'Invalid integer value for {name}: {value!r}')
        num = int(stripped, 10)
        minimum = spec.get('min', spec.get('minimum'))
        maximum = spec.get('max', spec.get('maximum'))
        if minimum is not None and num < minimum:
            raise ConfigError(f'Value for {name} ({num}) is below minimum {minimum}')
        if maximum is not None and num > maximum:
            raise ConfigError(f'Value for {name} ({num}) exceeds maximum {maximum}')
    elif kind in ('bool', 'boolean'):
        if value.strip().lower() not in BOOL_VALUES:
            raise ConfigError(f'Invalid boolean value for {name}: {value!r}')
    elif kind == 'path':
        if not value.strip() or any(ord(ch) < 32 for ch in value):
            raise ConfigError(f'Invalid path value for {name}: {value!r}')
        if spec.get('absolute') and not os.path.isabs(value):
            raise ConfigError(f'Path for {name} must be absolute: {value!r}')
        if spec.get('must_exist') and not Path(value).exists():
            raise ConfigError(f'Declared path for {name} does not exist: {value!r}')
    elif kind == 'enum':
        if not isinstance(allowed, list) or not allowed:
            raise ConfigError(f'Schema enum key {name} must declare a non-empty values list')
        choices = [str(item) for item in allowed]
        if value not in choices:
            raise ConfigError(f'Invalid enum value for {name}: {value!r} (expected one of {choices})')
    elif kind in ('string', 'str'):
        if spec.get('non_empty') and not value:
            raise ConfigError(f'Configuration key {name} must not be empty')
        min_len = spec.get('minLength')
        if min_len is not None and len(value) < min_len:
            raise ConfigError(f'Configuration key {name} shorter than minLength {min_len}')
    else:
        raise ConfigError(f'Unsupported schema type {kind!r} for key {name}')

    if allowed is not None and kind != 'enum':
        choices = [str(item) for item in allowed]
        if value not in choices:
            raise ConfigError(f'Invalid value for {name}: {value!r} (expected one of {choices})')


def apply_schema(
    resolved: dict[str, str],
    schema: dict,
    process: Mapping[str, str],
    process_precedence: bool,
) -> dict[str, str]:
    """Apply defaults and type/required checks from a loaded JSON schema."""
    if not isinstance(schema, dict):
        raise ConfigError('Schema root must be a JSON object')

    properties = schema.get('properties', schema.get('keys', {}))
    if not isinstance(properties, dict):
        raise ConfigError('Schema "properties" must be a JSON object')

    required_top = schema.get('required', [])
    if not isinstance(required_top, list):
        raise ConfigError('Schema "required" must be a JSON array')
    required_set = set(required_top)

    if schema.get('additionalProperties', True) is False:
        unknown = [k for k in resolved if k not in properties]
        if unknown:
            raise ConfigError(f'Unknown configuration key(s) not allowed by schema: {", ".join(unknown)}')

    for name, spec in properties.items():
        if not KEY_RE.fullmatch(name):
            raise ConfigError(f'Invalid schema key name: {name!r}')
        if not isinstance(spec, dict):
            raise ConfigError(f'Schema entry for {name} must be a JSON object')

        if name not in resolved and process_precedence:
            proc_val = _lookup_mapping(process, name)
            if proc_val:
                resolved[name] = proc_val

        if name not in resolved and 'default' in spec and spec['default'] is not None:
            raw_default = spec['default']
            if isinstance(raw_default, bool):
                default_str = 'true' if raw_default else 'false'
            else:
                default_str = str(raw_default)
            resolved[name] = _expand_value(default_str, resolved, process)

        is_required = bool(spec.get('required', False)) or (name in required_set)
        if is_required and not resolved.get(name):
            raise ConfigError(f'Missing required configuration key: {name}')

        if name in resolved:
            _validate_value(name, resolved[name], spec)

    return resolved


def resolve_env(
    env_path: Path | None,
    process: Mapping[str, str] | None = None,
    *,
    process_precedence: bool = True,
    strict: bool = False,
    source_key: str | None = None,
    schema: dict | None = None,
) -> dict[str, str]:
    """Load, expand, and validate configuration values."""
    proc = os.environ if process is None else process
    if source_key is not None and not KEY_RE.fullmatch(source_key):
        raise ConfigError(f'Invalid source key name: {source_key!r}')

    entries: list[tuple[str, str]] = []
    file_exists = False
    if env_path is None:
        if strict:
            raise ConfigError('Configuration file required in --strict mode')
    else:
        if env_path.is_dir():
            raise ConfigError(f'Configuration path is a directory: {env_path}')
        try:
            content = env_path.read_text(encoding='utf-8-sig')
            file_exists = True
        except FileNotFoundError as exc:
            if strict:
                raise ConfigError(f'Configuration file does not exist: {env_path}') from exc
        except OSError as exc:
            raise ConfigError(f'Cannot read configuration file {env_path}: {exc}') from exc
        else:
            entries = parse_env_text(content)

    scope: dict[str, str] = dict(proc) if process_precedence else {}
    resolved: dict[str, str] = {}

    for name, raw_value in entries:
        if process_precedence:
            proc_val = _lookup_mapping(proc, name)
            if proc_val:
                if '\0' in proc_val:
                    raise ConfigError(f'NUL byte in process environment key: {name}')
                resolved[name] = proc_val
                scope[name] = proc_val
                continue
        expanded = _expand_value(raw_value, scope, proc)
        resolved[name] = expanded
        scope[name] = expanded

    if schema is not None:
        resolved = apply_schema(resolved, schema, proc, process_precedence)

    if source_key is not None:
        resolved[source_key] = str(env_path.resolve()) if (file_exists and env_path is not None) else 'defaults'

    return resolved


def _systemd_quote(text: str) -> str:
    if '\n' in text or '\r' in text:
        raise ConfigError('Newline is not supported in systemd Environment= value')
    escaped = text.replace('\\', '\\\\').replace('"', '\\"').replace('%', '%%')
    return f'"{escaped}"'


def _xml_attr_quote(text: str) -> str:
    escaped = xml_escape(text, {'"': '&quot;', '\r': '&#13;', '\n': '&#10;', '\t': '&#9;'})
    return f'"{escaped}"'


def format_output(values: Mapping[str, str], fmt: str) -> str:
    """Render resolved environment mapping in the requested output format."""
    if fmt == 'json':
        return json.dumps(dict(values), indent=2)
    if fmt == 'shell':
        return '\n'.join(f'export {name}={shlex.quote(val)}' for name, val in values.items())
    if fmt == 'systemd':
        return '\n'.join(f'Environment={_systemd_quote(f"{name}={val}")}' for name, val in values.items())
    if fmt == 'iis':
        lines = ['<environmentVariables>']
        for name, val in values.items():
            lines.append(f'  <add name={_xml_attr_quote(name)} value={_xml_attr_quote(val)} />')
        lines.append('</environmentVariables>')
        return '\n'.join(lines)
    if fmt == 'msbuild':
        lines = ['<Project>', '  <PropertyGroup>']
        for name, val in values.items():
            lines.append(f'    <{name}>{xml_escape(val)}</{name}>')
        lines.extend(['  </PropertyGroup>', '</Project>'])
        return '\n'.join(lines)
    raise ConfigError(f'Unsupported format: {fmt}')


def _parse_bool_toggle(raw: str) -> bool:
    norm = raw.strip().lower()
    if norm in ('on', 'true', '1', 'yes'):
        return True
    if norm in ('off', 'false', '0', 'no'):
        return False
    raise argparse.ArgumentTypeError(f'expected on/off, got {raw!r}')


def parse_cli(argv: list[str]) -> argparse.Namespace:
    parser = _UsageParser(prog='machine-env', description='Load, expand, validate, and format env declarations')
    parser.add_argument('--file', default=None, help='Path to declarative env file')
    parser.add_argument('--strict', action='store_true', help='Fail if --file is omitted or does not exist')
    parser.add_argument(
        '--process-precedence',
        nargs='?',
        const=True,
        default=True,
        type=_parse_bool_toggle,
        metavar='on|off',
        help='Whether non-empty process env vars override file declarations (default: on)',
    )
    parser.add_argument('--no-process-precedence', dest='process_precedence', action='store_false',
                        help='Disable process env precedence')
    parser.add_argument('--schema', default=None, help='Optional JSON schema path')
    parser.add_argument('--source-key', default=None, metavar='NAME',
                        help='Optional metadata key name recording the resolved file path or "defaults"')
    parser.add_argument('--format', choices=('shell', 'json', 'systemd', 'iis', 'msbuild'), default='shell',
                        help='Output format (default: shell)')

    args = parser.parse_args(argv)
    if args.source_key is not None and not KEY_RE.fullmatch(args.source_key):
        parser.error(f'--source-key must be a valid identifier, got {args.source_key!r}')
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_cli(sys.argv[1:] if argv is None else argv)

    schema_obj: dict | None = None
    if args.schema is not None:
        schema_path = Path(args.schema)
        try:
            schema_obj = json.loads(schema_path.read_text(encoding='utf-8-sig'))
        except (OSError, ValueError) as exc:
            print(f'machine-env: cannot load schema {schema_path}: {exc}', file=sys.stderr)
            return EXIT_ERROR

    env_path = Path(args.file) if args.file is not None else None
    try:
        resolved = resolve_env(
            env_path,
            os.environ,
            process_precedence=args.process_precedence,
            strict=args.strict,
            source_key=args.source_key,
            schema=schema_obj,
        )
        rendered = format_output(resolved, args.format)
    except ConfigError as exc:
        print(f'machine-env: {exc}', file=sys.stderr)
        return EXIT_ERROR

    if rendered:
        print(rendered)
    return EXIT_OK


if __name__ == '__main__':
    raise SystemExit(main())
